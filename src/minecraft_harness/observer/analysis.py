"""Streaming paper metrics backed by disposable SQLite aggregation.

Client chat/death copies never contribute authoritative world counts. All
heuristics and incomplete fields are explicit; no causal cooperation score.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from ..evaluation.collector import artifact_bytes, iter_registered_evidence
from ..evaluation.logs import LOG_VERSION, CHAT_VERSION
from ..usage import normalize_usage, estimate_cost

METRIC_VERSION='paper-metrics-v1'
TERMINALS={'succeeded','failed','timed_out','cancelled','interrupted','rejected','invalidated'}


def stamp(value):
    try: return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError): return None


def payload_record(event):
    value=event.get('payload',{})
    for _ in range(4):
        if not isinstance(value,dict): return {}
        if isinstance(value.get('record'),dict): return value['record']
        if isinstance(value.get('job'),dict): return value['job']
        if isinstance(value.get('payload'),dict): value=value['payload']
        else: return value
    return value


def status_of(event, record):
    suffix=event['kind'].removeprefix('execution_').removeprefix('program_')
    if suffix in TERMINALS: return suffix
    if event['kind'] in {'execution_finished','program_finished','execution_completed'}:
        value=record.get('status','succeeded' if event['kind']=='execution_completed' else None)
        return value if value in TERMINALS else None
    return None


class Analysis:
    def __init__(self,run_dir,config,evaluation):
        self.run_dir=Path(run_dir)
        self.config=config
        self.evaluation=evaluation
        self.db=sqlite3.connect('')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.executescript('''
          CREATE TABLE executions(agent TEXT,id TEXT,start REAL,end REAL,status TEXT,hash TEXT,seq INTEGER,created REAL,PRIMARY KEY(agent,id));
          CREATE TABLE jobs(agent TEXT,id TEXT,start REAL,end REAL,status TEXT,seq INTEGER,created REAL,PRIMARY KEY(agent,id));
          CREATE TABLE memory_events(agent TEXT,tool TEXT,time REAL,seq INTEGER PRIMARY KEY);
          CREATE TABLE requests(agent TEXT,request TEXT,execution TEXT,PRIMARY KEY(agent,request));
          CREATE TABLE code(hash TEXT PRIMARY KEY,lines INTEGER,complete INTEGER);
          CREATE TABLE lines(program TEXT,line TEXT,PRIMARY KEY(program,line));
          CREATE TABLE usages(id TEXT PRIMARY KEY,agent TEXT,role TEXT,purpose TEXT,time REAL,seq INTEGER,usage TEXT,latency REAL,priority INTEGER,legacy INTEGER);
          CREATE TABLE facts(kind TEXT,agent TEXT,time REAL,seq INTEGER,detail TEXT,PRIMARY KEY(kind,agent,seq,detail));
          CREATE TABLE stages(agent TEXT,stage TEXT,time REAL,seq INTEGER,source TEXT,PRIMARY KEY(agent,stage));
          CREATE TABLE baseline_stages(agent TEXT,stage TEXT,PRIMARY KEY(agent,stage));
          CREATE TABLE ledger_times(seq INTEGER PRIMARY KEY,time REAL);
        ''')
        self.first_start=self.last_time=None
        self.finished_at=None
        self.success_time=None
        self.success_seq=evaluation.get('first_success_event_seq')
        self.counts=Counter()
        self.memory=Counter()
        self.agents=set(config.get('agents',{}).get('ids',[]))
        self.player_by_uuid={str(uuid.UUID(bytes=hashlib.md5(('OfflinePlayer:'+agent).encode()).digest(),version=3)):agent for agent in self.agents}
        self.incomplete=Counter()
        self.log_evidence=False
        self.stage_limit=1000
        self.event_count=0
        self.rates=config.get('budgets',{}).get('prices_per_million',{})
        database=self.run_dir/'events.sqlite3'
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
                for key in ('first_success_event_seq','first_verified_success','first_success'):
                    row=connection.execute('SELECT value_json FROM state WHERE key=?',(key,)).fetchone()
                    if row:
                        value=json.loads(row[0]); value=value.get('event_seq',value.get('first_success_event_seq')) if isinstance(value,dict) else value
                        if type(value) is int and value>0:
                            self.success_seq=min(self.success_seq,value) if self.success_seq else value
                if self.success_seq:
                    row=connection.execute('SELECT received_at FROM events WHERE event_seq=?',(self.success_seq,)).fetchone()
                    if row: self.success_time=stamp(row[0])

    def close(self): self.db.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

    def _code(self,digest):
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest): return
        if self.db.execute('SELECT 1 FROM code WHERE hash=?',(digest,)).fetchone(): return
        self.db.execute('INSERT INTO code VALUES (?,NULL,0)',(digest,))
        try:
            if (self.run_dir/'artifacts'/digest).stat().st_size>2_000_000:
                self.incomplete['oversized_code_artifact']+=1; return
            raw=artifact_bytes(self.run_dir,digest)
            if len(raw)>2_000_000:
                self.incomplete['oversized_code_artifact']+=1; return
            lines=raw.decode('utf-8').splitlines()
            meaningful=[line.strip() for line in lines if len(line.strip())>=12]
            complete=len(meaningful)<=4096
            self.db.execute('UPDATE code SET lines=?,complete=? WHERE hash=?',(len(lines),int(complete),digest))
            self.db.executemany('INSERT OR IGNORE INTO lines VALUES (?,?)',((digest,hashlib.sha256(line.encode()).hexdigest()) for line in meaningful[:4096]))
            if not complete: self.incomplete['code_line_hashes_capped']+=1
        except (OSError,ValueError,UnicodeError): self.incomplete['unavailable_code_artifact']+=1

    def event(self,event):
        self.event_count+=1
        now=stamp(event.get('received_at'))
        if now is not None: self.last_time=max(self.last_time or now,now)
        if event.get('kind') in {'run_finished','run_stopped'} and now is not None:
            self.finished_at=max(self.finished_at or now,now)
        if event.get('event_seq')==self.success_seq: self.success_time=now
        if event.get('source')=='agent_claim': return
        kind=event['kind']; payload=event.get('payload',{}); record=payload_record(event)
        if kind in {'server_log_collection_failed','server_log_unavailable'}:
            self.incomplete[kind]+=1
        if kind=='server_world_snapshot':
            self.db.execute('INSERT OR REPLACE INTO ledger_times VALUES (?,?)',(event['event_seq'],now))
        agent=event.get('actor_id','unknown')
        if agent not in {'supervisor','server_collector','observer'}: self.agents.add(agent)
        if kind in {'model_admitted','model_usage','model_response','model_completed'}:
            identity=payload.get('reservation_id') or event.get('model_call_id')
            legacy=not identity
            identity=identity or f"legacy:{kind}:{event['event_seq']}"
            priority=3 if kind=='model_usage' else 2 if kind in {'model_response','model_completed'} else 1
            usage=payload.get('normalized_usage',payload.get('usage',{}))
            latency=payload.get('latency_ms')
            latency=latency/1000 if isinstance(latency,(int,float)) and not isinstance(latency,bool) and math.isfinite(latency) and latency>=0 else None
            old=self.db.execute('SELECT usage,latency,priority,time,seq,agent,role,purpose FROM usages WHERE id=?',(identity,)).fetchone()
            encoded=json.dumps(usage)
            role=payload.get('role','observer' if agent=='observer' else 'player')
            purpose=payload.get('purpose','observer_review' if agent=='observer' else 'agent_decision')
            seq=event['event_seq']
            if old:
                if old[2]>priority:
                    encoded=old[0];agent,role,purpose=old[5:8]
                latency=latency if latency is not None else old[1]
                now=min(old[3],now) if old[3] is not None and now is not None else old[3] if old[3] is not None else now
                seq=min(seq,old[4])
                priority=max(priority,old[2])
            self.db.execute('INSERT OR REPLACE INTO usages VALUES (?,?,?,?,?,?,?,?,?,?)',(identity,agent,role,purpose,now,seq,encoded,latency,priority,int(legacy)))
        if kind=='tool_result' and payload.get('tool') in {'memory_read','memory_list','memory_write','history_read'}:
            self.memory[(agent,payload['tool'])]+=1
            self.db.execute('INSERT OR IGNORE INTO memory_events VALUES (?,?,?,?)',(agent,payload['tool'],now,event['event_seq']))
        if kind.startswith('job_'):
            identifier=record.get('job_id')
            if identifier:
                started=stamp(record.get('started_at'))
                if started is None and kind=='job_started': started=stamp(payload.get('received_at')) or now
                status=kind.removeprefix('job_')
                if kind=='job_finished': status=record.get('status')
                status=status if status in TERMINALS else None
                ended=stamp(record.get('finished_at')) or (now if status else None)
                previous=self.db.execute('SELECT start,end,status,created FROM jobs WHERE agent=? AND id=?',(agent,identifier)).fetchone()
                if previous:
                    started=previous[0] if previous[0] is not None else started
                    ended=ended if ended is not None else previous[1]
                    status=status or previous[2]
                created=previous[3] if previous else now
                self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',(agent,identifier,started,ended,status,event['event_seq'],created))
            if record.get('evidence_incomplete'):
                self.incomplete['background_output_truncated']+=1
        if kind.startswith(('execution_','program_')):
            identifier=event.get('execution_id') or record.get('execution_id') or payload.get('execution_id')
            request=record.get('request_id') or payload.get('request_id')
            if request:
                prior=self.db.execute('SELECT execution FROM requests WHERE agent=? AND request=?',(agent,request)).fetchone()
                if not identifier and prior: identifier=prior[0]
            if identifier or request:
                key=identifier or 'request:'+request
                if identifier and request:
                    provisional=self.db.execute('SELECT start,end,status,hash,seq,created FROM executions WHERE agent=? AND id=?',(agent,'request:'+request)).fetchone()
                    if provisional:
                        self.db.execute('INSERT OR IGNORE INTO executions VALUES (?,?,?,?,?,?,?,?)',(agent,key,*provisional))
                        self.db.execute('DELETE FROM executions WHERE agent=? AND id=?',(agent,'request:'+request))
                    self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?)',(agent,request,identifier))
                started=stamp(record.get('started_at'))
                if started is None and kind in {'execution_running','program_running','execution_started','program_started'}:
                    started=stamp(payload.get('received_at')) or now
                ended=stamp(record.get('finished_at'))
                status=status_of(event,record)
                if ended is None and status: ended=now
                source_ref=record.get('source_ref') or payload.get('source_ref')
                digest=record.get('source_hash') or (source_ref.get('sha256') if isinstance(source_ref,dict) else None)
                self._code(digest)
                previous=self.db.execute('SELECT start,end,status,hash,created FROM executions WHERE agent=? AND id=?',(agent,key)).fetchone()
                if previous:
                    started=previous[0] if previous[0] is not None else started
                    ended=ended if ended is not None else previous[1]
                    status=status or previous[2]; digest=digest or previous[3]
                created=previous[4] if previous and previous[4] is not None else now
                if created is not None and started is not None: created=min(created,started)
                self.db.execute('INSERT OR REPLACE INTO executions VALUES (?,?,?,?,?,?,?,?)',(agent,key,started,ended,status,digest,event['event_seq'],created))
                if started is not None: self.first_start=min(self.first_start or started,started)
                cleanup=record.get('cleanup') or {}
                cleanup=cleanup if isinstance(cleanup,dict) else {}
                if record.get('evidence_incomplete') or record.get('output_truncated') or record.get('result_truncated') or cleanup.get('logs_truncated') or cleanup.get('logs_suppressed'):
                    self.incomplete['execution_output_truncated']+=1

    def logs(self):
        baseline_seen=False
        for row in iter_registered_evidence(self.run_dir):
            if row['kind']=='server_world_snapshot':
                try:
                    document=json.loads(artifact_bytes(self.run_dir,row['artifact_sha256']))
                    for relative,digest in document.get('files',{}).items():
                        if not relative.startswith('advancements/'): continue
                        player_uuid=Path(relative).stem
                        actor=self.player_by_uuid.get(player_uuid,player_uuid)
                        advancement=json.loads(artifact_bytes(self.run_dir,digest))
                        for name,entry in advancement.items():
                            if not isinstance(entry,dict) or entry.get('done') is not True: continue
                            if row['baseline'] and not baseline_seen:
                                self.db.execute('INSERT OR IGNORE INTO baseline_stages VALUES (?,?)',(actor,name))
                            elif not self.db.execute('SELECT 1 FROM baseline_stages WHERE agent=? AND stage=?',(actor,name)).fetchone():
                                times=[stamp(value) for value in entry.get('criteria',{}).values() if isinstance(value,str)]
                                times=[value for value in times if value is not None]
                                observed=self.db.execute('SELECT time FROM ledger_times WHERE seq=?',(row['event_seq'],)).fetchone()
                                when=max(times) if times else observed[0] if observed else None
                                reference={'event_seq':row['event_seq'],'evidence_id':row['evidence_id'],
                                           'artifact_sha256':digest,'world_epoch':row['world_epoch'],
                                           'relative_path':relative,'time_basis':'last_recorded_criterion_upper_bound' if times else 'snapshot_observation_upper_bound'}
                                self.db.execute('INSERT OR IGNORE INTO stages VALUES (?,?,?,?,?)',(actor,name,when,row['event_seq'],json.dumps(reference)))
                    if row['baseline']: baseline_seen=True
                except (OSError,ValueError,KeyError,TypeError): self.incomplete['invalid_stage_snapshot']+=1
                continue
            if row['kind']=='server_log_gap':
                self.incomplete['server_log_gap']+=1; continue
            if row['kind']!='server_log_segment': continue
            self.log_evidence=True
            try:
                document=json.loads(artifact_bytes(self.run_dir,row['artifact_sha256']))
                raw=artifact_bytes(self.run_dir,document['raw_sha256'])
                if len(raw)!=document['offset_end']-document['offset_start']: raise ValueError('length mismatch')
                if document.get('normalizer_version')!=LOG_VERSION:
                    self.incomplete['legacy_unnormalized_log']+=1; continue
                if not document.get('complete'): self.incomplete['incomplete_log_segment']+=1
                for item in document.get('events',[]):
                    if item.get('kind') not in {'chat','death','advancement'}: continue
                    actor=item.get('actor_id') or 'unknown'
                    moment=stamp(item.get('source_time'))
                    if item.get('time_warnings'): self.incomplete['source_clock_uncertain']+=1
                    detail={**item,'evidence_ref':{'event_seq':row['event_seq'],'artifact_sha256':row['artifact_sha256'],'evidence_id':row['evidence_id']}}
                    self.db.execute('INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?)',(item['kind'],actor,moment,row['event_seq'],json.dumps(detail,ensure_ascii=False)))
                    if item['kind']=='advancement':
                        self.db.execute('INSERT OR IGNORE INTO stages VALUES (?,?,?,?,?)',(actor,'server_title:'+item['title'],moment,row['event_seq'],json.dumps(detail['evidence_ref'])))
            except (OSError,ValueError,KeyError,TypeError): self.incomplete['invalid_server_log_evidence']+=1
        registry=self.run_dir/'evaluation'/'server_evidence.sqlite3'
        if registry.is_file() and not registry.is_symlink():
            with closing(sqlite3.connect(registry.as_uri()+'?mode=ro',uri=True)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='log_cursors'").fetchone():
                    for encoded, in connection.execute('SELECT value_json FROM log_cursors'):
                        cursor=json.loads(encoded);pending=cursor.get('pending_bytes')
                        if type(pending) is int and pending>0:self.incomplete['unarchived_server_log_bytes']+=pending
                        elif pending is None:self.incomplete['server_log_tail_unobserved']+=1
                elif self.log_evidence:self.incomplete['server_log_tail_unobserved']+=1

    def _usage(self,cutoff):
        groups={}
        query='SELECT agent,role,purpose,usage,latency,legacy,priority FROM usages WHERE (? IS NULL OR time<=?)'
        # For historical records without call IDs, keep settlement authority and
        # suppress that actor's response copies; expose the ambiguity explicitly.
        legacy_actors={row[0] for row in self.db.execute('SELECT DISTINCT agent FROM usages WHERE legacy=1 AND priority=3')}
        for agent,role,purpose,encoded,latency,legacy,priority in self.db.execute(query,(cutoff,cutoff)):
            if legacy and priority==2 and agent in legacy_actors: continue
            if legacy and priority==1: continue
            key=(agent,role,purpose)
            if key not in groups: groups[key]={'actor_id':agent,'role':role,'purpose':purpose,'calls':0,'known':Counter(),'unknown':Counter(),'cost_components':Counter(),
                'inclusion_conventions':Counter(),'usage_warnings':Counter(),'cost_methods':Counter()}
            group=groups[key]; group['calls']+=1
            usage=normalize_usage(json.loads(encoded)); cost=estimate_cost(usage,self.rates)
            values={name:usage.get(name) for name in ('input_tokens','output_tokens','cached_input_tokens','noncached_input_tokens','reasoning_tokens','visible_output_tokens')}
            values.update(estimated_cost_usd=cost['cost_usd'],model_wait_s=latency)
            for field in ('cached_input_included','reasoning_included_in_output'):
                flag=usage.get(field)
                group['inclusion_conventions'][field+':'+('unknown' if flag is None else str(flag).lower())]+=1
            group['usage_warnings'].update(usage.get('warnings',[]))
            group['cost_methods'][cost['method']]+=1
            for name,value in values.items():
                if value is None: group['unknown'][name]+=1
                else: group['known'][name]+=value
            for name,value in cost['components'].items():
                if value is not None: group['cost_components'][name]+=value
        rendered=[]
        for group in groups.values():
            known=group.pop('known'); unknown=group.pop('unknown')
            group.update({name:None if unknown[name] else known[name] for name in ('input_tokens','output_tokens','cached_input_tokens','noncached_input_tokens','reasoning_tokens','visible_output_tokens','estimated_cost_usd','model_wait_s')})
            group['unknown_calls_by_field']=dict(unknown); group['known_partial_sums']=dict(known)
            group['cost_components']=dict(group['cost_components']); rendered.append(group)
        return {'groups':sorted(rendered,key=lambda item:(item['role'],item['purpose'],item['actor_id'])),
                'legacy_unlinked_usage':bool(legacy_actors),'usage_version':'usage-v2',
                'rates_per_million':self.rates,'currency':'USD','window_policy':'calls are assigned by first recorded admission/response time, including planning before the first program',
                'cost_policy':'component subsets are split, never added to their parent totals; unknown quantities remain null'}

    def _window(self,cutoff):
        end=cutoff if cutoff is not None else (self.finished_at if self.finished_at is not None else self.last_time)
        duration=max(0,end-self.first_start) if end is not None and self.first_start is not None else None
        log_complete=self.log_evidence and not any(self.incomplete[key] for key in ('server_log_gap','legacy_unnormalized_log','invalid_server_log_evidence','incomplete_log_segment','source_clock_uncertain','unarchived_server_log_bytes','server_log_collection_failed','server_log_unavailable','server_log_tail_unobserved'))
        players=[]
        for agent in sorted(self.agents):
            statuses=Counter(); total_duration=0; known_count=0; missing_duration=0;censored_count=0;not_started=0;observed_runtime=0
            active=0; interval_start=interval_end=None
            for start,finish,status in self.db.execute('SELECT start,end,status FROM executions WHERE agent=? AND (? IS NULL OR created<=?) ORDER BY start',(agent,cutoff,cutoff)):
                if cutoff is not None and start is not None and start>cutoff:
                    statuses['not_started_at_cutoff']+=1;not_started+=1;continue
                if cutoff is None or finish is not None and finish<=cutoff: statuses[status or 'unresolved']+=1
                else: statuses['in_progress_at_cutoff']+=1
                if start is None:
                    missing_duration+=1; continue
                stop=min(finish,end) if finish is not None and end is not None else end
                if stop is None: missing_duration+=1; continue
                elapsed=max(0,stop-start)
                observed_runtime+=elapsed
                if finish is not None and end is not None and finish<=end:
                    total_duration+=elapsed;known_count+=1
                else: censored_count+=1
                if interval_start is None: interval_start,interval_end=start,stop
                elif start<=interval_end: interval_end=max(interval_end,stop)
                else: active+=max(0,interval_end-interval_start);interval_start,interval_end=start,stop
            if interval_start is not None: active+=max(0,interval_end-interval_start)
            job_statuses=Counter(); job_duration=0; job_known=job_unknown=0
            for start,finish,status in self.db.execute('SELECT start,end,status FROM jobs WHERE agent=? AND (? IS NULL OR created<=?)',(agent,cutoff,cutoff)):
                job_statuses[status or 'unresolved' if cutoff is None or finish is not None and finish<=cutoff else 'in_progress_at_cutoff']+=1
                if start is None or end is None: job_unknown+=1; continue
                job_duration+=max(0,min(finish,end)-start if finish is not None else end-start);job_known+=1
            # Activity uses the union, so detached job time cannot be added
            # twice when it overlaps a foreground program or another job.
            combined=0; union_start=union_end=None
            query='''SELECT start,end FROM (SELECT agent,start,end,created FROM executions UNION ALL SELECT agent,start,end,created FROM jobs)
                     WHERE agent=? AND start IS NOT NULL AND (? IS NULL OR created<=?) ORDER BY start'''
            for start,finish in self.db.execute(query,(agent,cutoff,cutoff)):
                if end is None: continue
                finish=min(finish,end) if finish is not None else end
                if start>end: continue
                if union_start is None: union_start,union_end=start,finish
                elif start<=union_end: union_end=max(union_end,finish)
                else: combined+=max(0,union_end-union_start);union_start,union_end=start,finish
            if union_start is not None: combined+=max(0,union_end-union_start)
            quantiles={}
            for label,fraction in (('p50',.5),('p95',.95),('p99',.99)):
                if known_count and end is not None:
                    query='SELECT MAX(0,end-start) AS duration FROM executions WHERE agent=? AND start IS NOT NULL AND end IS NOT NULL AND end<=? AND (? IS NULL OR created<=?) ORDER BY duration LIMIT 1 OFFSET ?'
                    row=self.db.execute(query,(agent,end,cutoff,cutoff,min(known_count-1,int(known_count*fraction)))).fetchone()
                    quantiles[label]=row[0] if row else None
                else: quantiles[label]=None
            death=Counter()
            chat=Counter();chat_count=0
            for kind,detail in self.db.execute('SELECT kind,detail FROM facts WHERE agent=? AND (? IS NULL OR time<=?) AND (? IS NULL OR time>=?)',(agent,cutoff,cutoff,self.first_start,self.first_start)):
                item=json.loads(detail)
                if kind=='death': death[item['cause']]+=1
                elif kind=='chat': chat.update(item['classification']['labels']);chat_count+=1
            players.append({'actor_id':agent,'execution_status_counts':dict(statuses),
                'program_duration_s':{'known_count':known_count,'unknown_count':missing_duration,'censored_count':censored_count,'not_started_at_cutoff':not_started,'sum':total_duration,
                    'mean':total_duration/known_count if known_count else None,
                    **quantiles,'distribution_complete':missing_duration==0 and censored_count==0,
                    'quantile_policy':'exact ordered completed durations in disk-backed SQLite; missing and window-censored programs excluded and counted; receipt-time terminal bounds used when native end absent'},
                'program_observed_runtime_s':observed_runtime,
                'active_s':active,'active_fraction':min(1,active/duration) if duration else None,
                'active_time_complete':missing_duration==0,
                'background_status_counts':dict(job_statuses),
                'background_duration_s':{'known_count':job_known,'unknown_count':job_unknown,'sum':job_duration},
                'combined_active_s':combined,'combined_active_fraction':min(1,combined/duration) if duration else None,
                'error_count':statuses['failed'],'timeout_count':statuses['timed_out'],
                'infrastructure_interruption_count':statuses['interrupted']+statuses['invalidated'],
                'death_count':sum(death.values()) if self.log_evidence else None,'death_causes':dict(death) if self.log_evidence else None,
                'death_count_complete':log_complete,'chat_message_count':chat_count if self.log_evidence else None,
                'chat_labels':dict(chat),'memory_tool_calls':dict(self.db.execute('SELECT tool,COUNT(*) FROM memory_events WHERE agent=? AND (? IS NULL OR time<=?) GROUP BY tool',(agent,cutoff,cutoff)))})
        facts=Counter(row[0] for row in self.db.execute('SELECT kind FROM facts WHERE (? IS NULL OR time<=?) AND (? IS NULL OR time>=?)',(cutoff,cutoff,self.first_start,self.first_start)))
        samples={}
        for kind in ('death','chat','advancement'):
            samples[kind]=[]
            for encoded, in self.db.execute('SELECT detail FROM facts WHERE kind=? AND (? IS NULL OR time<=?) AND (? IS NULL OR time>=?) ORDER BY time LIMIT 16',(kind,cutoff,cutoff,self.first_start,self.first_start)):
                item=json.loads(encoded)
                samples[kind].append({name:item.get(name) for name in ('actor_id','source_time','received_at','world_epoch','source','evidence_ref','cause','classification','title') if name in item})
        stage_count=self.db.execute('SELECT COUNT(*) FROM stages WHERE (? IS NULL OR time<=?) AND (? IS NULL OR time>=?)',(cutoff,cutoff,self.first_start,self.first_start)).fetchone()[0]
        return {'origin':'first_authoritative_program_start','origin_time':self.first_start,'end_time':end,
                'duration_s':duration,'players':players,'authoritative_server_counts':dict(facts) if self.log_evidence else None,
                'fact_evidence_samples':samples,'fact_evidence_omitted':{kind:max(0,facts[kind]-len(samples[kind])) for kind in samples},
                'stage_observation_count':stage_count,'stage_timing_unknown_count':self.db.execute('SELECT COUNT(*) FROM stages WHERE time IS NULL').fetchone()[0],
                'server_evidence_complete':log_complete,
                'incomplete_scope':'collection-wide uncertainty is conservatively retained in both windows',
                'reuse':self._reuse(cutoff),
                'costs':self._usage(cutoff),'incomplete':dict(self.incomplete),'complete':not bool(self.incomplete)}

    def _reuse(self,cutoff):
        eligible='SELECT agent,hash FROM executions WHERE hash IS NOT NULL AND (? IS NULL OR created<=?)'
        parameters=(cutoff,cutoff)
        repeated=self.db.execute('SELECT COUNT(*),COALESCE(SUM(n-1),0) FROM (SELECT hash,COUNT(*) n FROM ('+eligible+') GROUP BY hash HAVING n>1)',parameters).fetchone()
        cross=self.db.execute('SELECT COUNT(*) FROM (SELECT hash FROM ('+eligible+') GROUP BY hash HAVING COUNT(DISTINCT agent)>1)',parameters).fetchone()[0]
        line=self.db.execute('SELECT COUNT(*) FROM (SELECT line FROM lines JOIN ('+eligible+') e ON e.hash=lines.program GROUP BY line HAVING COUNT(DISTINCT program)>1)',parameters).fetchone()[0]
        cross_line=self.db.execute('SELECT COUNT(*) FROM (SELECT line FROM lines JOIN ('+eligible+') e ON e.hash=lines.program GROUP BY line HAVING COUNT(DISTINCT agent)>1)',parameters).fetchone()[0]
        return {'version':'exact-code-lines-v1','repeated_program_hashes':repeated[0],'repeated_execution_count':repeated[1],
                'cross_agent_program_hashes':cross,'line_hashes_in_multiple_programs':line,'cross_agent_line_hashes':cross_line,
                'line_policy':'SHA256 of whitespace-trimmed UTF-8 lines with at least 12 characters; at most 4096 lines per distinct source; skipped data marked incomplete',
                'interpretation':'exact lexical equality; cannot establish copying or causal benefit'}

    def finish(self):
        self.logs()
        self.db.commit()
        if self.first_start is None: self.incomplete['unknown_program_origin']+=1
        if not self.log_evidence: self.incomplete['server_log_unavailable']+=1
        stages=[]
        stage_filter=' WHERE (? IS NULL OR time IS NULL OR time>=?)'
        for actor,stage,moment,seq,source in self.db.execute('SELECT * FROM stages'+stage_filter+' ORDER BY time LIMIT ?',(self.first_start,self.first_start,self.stage_limit)):
            stages.append({'actor_id':actor,'stage':stage,'source_time':moment,'event_seq':seq,'evidence_ref':json.loads(source)})
        stage_count=self.db.execute('SELECT COUNT(*) FROM stages'+stage_filter,(self.first_start,self.first_start)).fetchone()[0]
        return {'version':METRIC_VERSION,'source_time_policy':'server HH:MM:SS date is anchored to file mtime; midnight rollover tracked, uncertainty retained',
            'full_session':self._window(None),
            'pre_first_success':self._window(self.success_time) if self.success_time is not None else None,
            'first_success':{'event_seq':self.success_seq,'observed_at':self.success_time,'precision':'verified snapshot observation upper bound'} if self.success_seq else None,
            'chat_classification':{'version':CHAT_VERSION,'multi_label':True,'causal_claim':False,
                'labels':['warning','rescue','shared_storage','lesson','coordinates'],'method':'deterministic lexical rules, not semantic interpretation'},
            'memory_policy':'counts actual memory/history tool results; cannot infer that read content was used or useful',
            'death_classification':{'version':'death-message-rules-v1','policy':'lexical category of authoritative English Vanilla death message; raw bytes retained, no inferred causal chain'},
            'reuse':self._reuse(None),
            'stage_timeline':stages,'stage_timeline_omitted':max(0,stage_count-len(stages)),
            'stage_policy':'only explicit registered server evidence; absent milestones remain unknown'}
