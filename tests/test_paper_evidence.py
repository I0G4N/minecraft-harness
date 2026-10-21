"""Paper measurements use authoritative bytes, stable call IDs and honest unknowns."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from minecraft_harness.budget import Budget, BudgetExhausted
from minecraft_harness.evaluation.collector import ServerEvidenceCollector, artifact_bytes, registered_evidence
from minecraft_harness.evaluation.logs import normalize_segment
from minecraft_harness.observer import build_report, write_report
from minecraft_harness.recorder import Recorder
from minecraft_harness.usage import estimate_cost, normalize_usage, reservation_cost


RATES={'input':2,'cached_input':.5,'output':4,'reasoning':3}


def usage(inp=100,out=50,cached=40,reasoning=20):
    return {'input_tokens':inp,'output_tokens':out,
            'input_tokens_details':{'cached_tokens':cached},'output_tokens_details':{'reasoning_tokens':reasoning}}


class UsageComponentsTests(unittest.TestCase):
    def test_deepseek_cache_fields_are_subsets_and_cost_is_split_once(self):
        raw = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
               'prompt_cache_hit_tokens': 40, 'prompt_cache_miss_tokens': 60,
               'completion_tokens_details': {'reasoning_tokens': 20}}
        normalized = normalize_usage(raw)
        self.assertEqual((normalized['input_tokens'], normalized['cached_input_tokens'], normalized['noncached_input_tokens']), (100, 40, 60))
        self.assertTrue(normalized['cached_input_included'])
        self.assertEqual(normalized, normalize_usage(normalized))
        self.assertAlmostEqual(estimate_cost(raw, RATES)['cost_usd'], .00032)
        # OpenAI-compatible details can accompany the provider aliases.
        raw['prompt_tokens_details'] = {'cached_tokens': 40}
        self.assertEqual(normalize_usage(raw), normalized)

    def test_deepseek_zero_cache_is_known_and_single_subtotal_can_be_reconciled(self):
        raw = {'prompt_tokens': 100, 'completion_tokens': 50,
               'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100}
        result = estimate_cost(raw, {'input': 2, 'cached_input': .5, 'output': 4})
        self.assertTrue(result['complete'])
        self.assertAlmostEqual(result['cost_usd'], .0004)
        for subtotal in [{'prompt_cache_hit_tokens': 40}, {'prompt_cache_miss_tokens': 60}]:
            with self.subTest(subtotal=subtotal):
                normalized = normalize_usage({'prompt_tokens': 100, 'completion_tokens': 50, **subtotal})
                self.assertEqual((normalized['cached_input_tokens'], normalized['noncached_input_tokens']), (40, 60))
        self.assertIsNone(normalize_usage({'prompt_tokens': 100, 'completion_tokens': 50})['cached_input_tokens'])

    def test_deepseek_invalid_or_conflicting_subtotals_do_not_become_zero(self):
        base = {'prompt_tokens': 100, 'completion_tokens': 50,
                'prompt_cache_hit_tokens': 40, 'prompt_cache_miss_tokens': 60}
        invalid = [
            {'prompt_cache_hit_tokens': value} for value in [None, True, -1, '40', 1.5, 2**1000]
        ] + [
            {'prompt_cache_miss_tokens': value} for value in [None, False, -1, '60', 1.5, 2**1000, 101]
        ] + [
            {'prompt_cache_hit_tokens': 41},
            {'prompt_tokens_details': {'cached_tokens': 0}},
            {'prompt_tokens_details': {'cached_tokens': False}},
            {'cached_input_tokens': None},
            {'cached_input_included': False},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                raw = {**base, **fields}; normalized = normalize_usage(raw)
                self.assertIsNone(normalized['input_tokens'])
                self.assertIsNone(normalized['cached_input_tokens'])
                self.assertIsNone(normalized['noncached_input_tokens'])
                self.assertTrue(normalized['warnings'])
                self.assertEqual(normalized, normalize_usage(normalized))
                self.assertIsNone(estimate_cost(raw, {'input': 2, 'cached_input': .5, 'output': 4})['cost_usd'])

    def test_standard_subsets_are_split_once_and_normalization_is_idempotent(self):
        normalized=normalize_usage(usage())
        self.assertEqual((normalized['input_tokens'],normalized['noncached_input_tokens'],normalized['visible_output_tokens']),(100,60,30))
        self.assertEqual(normalized,normalize_usage(normalized))
        result=estimate_cost(normalized,RATES)
        self.assertTrue(result['complete'])
        self.assertAlmostEqual(result['cost_usd'],.00032)
        self.assertAlmostEqual(sum(result['components'].values()),result['cost_usd'])
        self.assertAlmostEqual(reservation_cost(1000,100,{**RATES,'reasoning':6}),.0026)

    def test_exclusive_quantities_add_to_totals_once(self):
        normalized=normalize_usage({'input_tokens':60,'cache_read_input_tokens':40,
            'output_tokens':30,'reasoning_tokens':20,'reasoning_included_in_output':False})
        self.assertEqual((normalized['input_tokens'],normalized['output_tokens']),(100,50))
        self.assertEqual(normalized,normalize_usage(normalized))
        self.assertAlmostEqual(estimate_cost(normalized,RATES)['cost_usd'],.00032)

    def test_ambiguous_missing_and_invalid_components_stay_unknown(self):
        normalized=normalize_usage({'input_tokens':100,'output_tokens':50,'cached_input_tokens':40,'reasoning_tokens':20})
        self.assertIsNone(normalized['input_tokens']);self.assertIsNone(normalized['output_tokens'])
        self.assertIsNone(estimate_cost(normalized,RATES)['cost_usd'])
        self.assertIsNone(estimate_cost({'input_tokens':100,'output_tokens':50},RATES)['cost_usd'])
        self.assertAlmostEqual(estimate_cost({'input_tokens':100,'output_tokens':50},{'input':2,'output':4})['cost_usd'],.0004)
        invalid=normalize_usage(usage(cached=101,reasoning=51))
        self.assertIsNone(invalid['input_tokens']);self.assertIsNone(invalid['output_tokens'])
        self.assertEqual(len(invalid['warnings']),2)
        self.assertIsNone(normalize_usage({'usage_version':'usage-v2','input_tokens':-2})['input_tokens'])

    def test_provider_cost_is_not_added_to_an_estimate(self):
        result=estimate_cost({**usage(),'cost_usd':1.25},RATES)
        self.assertEqual(result['cost_usd'],1.25)
        self.assertEqual(result['method'],'provider_reported')
        self.assertEqual(result['components'],{})

    def test_adapter_null_aliases_do_not_invent_inclusion_and_absurd_counts_are_unknown(self):
        raw={'prompt_tokens':None,'completion_tokens':None,'input_tokens':100,'output_tokens':50,
             'cached_input_tokens':40,'reasoning_tokens':20}
        self.assertIsNone(normalize_usage(raw)['input_tokens'])
        self.assertIsNone(normalize_usage(raw)['output_tokens'])
        raw={'prompt_tokens':None,'input_tokens':60,'cache_read_input_tokens':40}
        self.assertEqual(normalize_usage(raw)['input_tokens'],100)
        self.assertIsNone(normalize_usage({'input_tokens':2**1000})['input_tokens'])
        created=normalize_usage({'input_tokens':100,'cache_creation_input_tokens':20,'output_tokens':5})
        self.assertIsNone(created['input_tokens'])
        self.assertIsNone(estimate_cost(created,{'input':1,'output':1})['cost_usd'])


class PurposeBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.run=Path(temporary.name);self.recorder=Recorder(self.run);self.addCleanup(self.recorder.close)
        self.config={'wall_time_minutes':10,'total_model_calls':10,'max_cost_usd':1,
                     'max_input_tokens':1000,'prices_per_million':RATES}

    async def test_every_role_and_purpose_uses_same_reservations_and_exact_settlement(self):
        budget=Budget(self.config,self.recorder,max_output_tokens=100)
        identities=[]
        for actor,role,purpose in [('alice','player','agent_decision'),('alice','player','context_compaction'),('observer','observer','observer_review')]:
            identity=await budget.reserve(actor,role=role,purpose=purpose);identities.append(identity)
            self.assertEqual(budget.state['pending'][identity]['reserved_cost'],.0024)
            await budget.settle(identity,usage())
        self.assertEqual(budget.state['calls'],3)
        self.assertEqual(budget.state['input_tokens'],300)
        self.assertAlmostEqual(budget.state['cost_usd'],.00096)
        events=[event for event in self.recorder.all_events() if event['kind']=='model_usage']
        self.assertEqual([event['payload']['purpose'] for event in events],['agent_decision','context_compaction','observer_review'])
        self.assertEqual({event['payload']['reservation_id'] for event in events},set(identities))

    async def test_missing_split_price_usage_charges_reservation_and_recovery_retains_purpose(self):
        budget=Budget(self.config,self.recorder)
        identity=await budget.reserve('observer',purpose='observer_review',role='observer')
        recovered=Budget(self.config,self.recorder)
        event=list(self.recorder.all_events())[-1]
        self.assertEqual(event['payload']['reservation_id'],identity)
        self.assertTrue(event['payload']['recovered_pending'])
        self.assertEqual(event['payload']['purpose'],'observer_review')
        self.assertAlmostEqual(recovered.state['cost_usd'],1000/1e6*2+4096/1e6*4)
        identity=await recovered.reserve('alice')
        await recovered.settle(identity,usage())
        self.assertFalse(recovered.state['pending'])
        self.assertEqual(Budget(self.config,self.recorder).state['unknown_usage_calls'],1)

    async def test_known_provider_cost_survives_unknown_tokens(self):
        budget=Budget(self.config,self.recorder)
        identity=await budget.reserve('alice')
        await budget.settle(identity,{'cost_usd':.125})
        self.assertEqual(budget.state['cost_usd'],.125)
        self.assertEqual(budget.state['unknown_usage_calls'],1)

    async def test_deepseek_settlement_keeps_paid_admission_open_and_bad_cache_stops_it(self):
        config = {**self.config, 'prices_per_million': {'input': 2, 'cached_input': .5, 'output': 4}}
        budget = Budget(config, self.recorder, max_output_tokens=100)
        first = await budget.reserve('alice')
        raw = {'prompt_tokens': 100, 'completion_tokens': 50,
               'prompt_cache_hit_tokens': 40, 'prompt_cache_miss_tokens': 60}
        await budget.settle(first, raw)
        self.assertEqual(budget.state.get('unknown_cost_calls', 0), 0)
        self.assertEqual(budget.state['unknown_usage_calls'], 0)
        self.assertAlmostEqual(budget.state['cost_usd'], .00034)
        second = await budget.reserve('observer', role='observer', purpose='observer_review')
        await budget.settle(second, {**raw, 'prompt_cache_miss_tokens': 0})
        # Ambiguous cache subtotals stay unknown but are charged at the
        # admission-time reservation instead of freezing paid admission.
        self.assertEqual(budget.state.get('unknown_cost_calls'), 1)
        self.assertAlmostEqual(budget.state['cost_usd'], .00034 + .0024)
        third = await budget.reserve('alice')
        await budget.settle(third, raw)
        self.assertFalse(budget.state['pending'])
        self.assertEqual(budget.state['unknown_usage_calls'], 1)


class LogEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.run=self.root/'run'
        self.recorder=Recorder(self.run);self.addCleanup(self.recorder.close)
        self.collector=ServerEvidenceCollector(self.run,self.recorder);self.log=self.root/'latest.log'
        self.mtime=datetime(2026,10,22,0,1,tzinfo=timezone.utc).timestamp()

    def documents(self,kind='server_log_segment'):
        return [json.loads(artifact_bytes(self.run,row['artifact_sha256'])) for row in registered_evidence(self.run) if row['kind']==kind]

    def test_midnight_offsets_timezone_multilabel_and_chat_spoof(self):
        raw=('[23:59:59] [Server thread/INFO]: <alice> careful help chest lesson x=12 y=64 z=-3\n'
             '[00:00:01] [Server thread/INFO]: alice tried to swim in lava\n'
             '[00:00:02] [Server thread/INFO]: <bob> alice was slain by Zombie\n').encode()
        normalized=normalize_segment(raw,cursor={},path='latest.log',generation='g',offset=13,world_epoch=2,mtime=self.mtime,source_timezone='UTC')
        self.assertTrue(normalized['complete'])
        self.assertEqual([item['kind'] for item in normalized['events']],['chat','death','chat'])
        first,death,last=normalized['events']
        self.assertEqual(first['source_time'],'2026-10-21T23:59:59+00:00')
        self.assertEqual(death['source_time'],'2026-10-22T00:00:01+00:00')
        self.assertEqual(first['classification']['labels'],['warning','rescue','shared_storage','lesson','coordinates'])
        self.assertEqual(death['cause'],'lava');self.assertEqual(death['world_epoch'],2)
        self.assertEqual(first['source']['offset_start'],13)
        self.assertEqual(last['source']['offset_end'],13+len(raw))
        self.assertTrue(first['received_at'])

    def test_rotation_drains_old_inode_tail_and_missing_inode_is_explicit_gap(self):
        self.log.write_text('[12:00:00] [Server thread/INFO]: <alice> first\n')
        self.collector.collect_log(self.log,world_epoch=0)
        with self.log.open('a') as stream: stream.write('[12:00:01] [Server thread/INFO]: alice fell from a high place\n')
        self.log.rename(self.root/'old.log')
        self.log.write_text('[12:00:02] [Server thread/INFO]: <bob> second\n')
        self.collector.collect_log(self.log,world_epoch=1)
        documents=self.documents()
        self.assertEqual([item['events'][0]['kind'] for item in documents],['chat','death','chat'])
        self.assertEqual([item['events'][0]['world_epoch'] for item in documents],[0,0,1])
        self.assertEqual(self.documents('server_log_gap'),[])
        self.log.unlink();self.log.write_text('[12:00:03] [Server thread/INFO]: bob drowned\n')
        self.collector.collect_log(self.log,world_epoch=2)
        gap=self.documents('server_log_gap')[0]
        self.assertIsNone(gap['lost_bytes'])
        self.assertIn(gap['reason'],{'rotated_inode_tail_unavailable','log_truncated_or_rewritten'})

    def test_concurrent_calls_share_cursor_and_chatty_chunks_do_not_drop_events(self):
        self.log.write_text(''.join(f'[12:00:00] [Server thread/INFO]: <alice> message {index}\n' for index in range(9000)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:self.collector.collect_log(self.log),range(4)))
        documents=self.documents()
        self.assertEqual(sum(len(document['events']) for document in documents),9000)
        self.assertTrue(all(document['complete'] for document in documents))
        self.assertTrue(all(len(document['events'])<=4096 for document in documents))
        self.assertEqual(documents[0]['offset_start'],0)
        for old,new in zip(documents,documents[1:]):self.assertEqual(old['offset_end'],new['offset_start'])

    def test_rotated_inode_that_was_truncated_cannot_claim_complete_tail(self):
        self.log.write_text('[12:00:00] [Server thread/INFO]: <alice> original long line\n')
        self.collector.collect_log(self.log)
        old=self.root/'old.log';self.log.rename(old)
        old.write_text('short')
        self.log.write_text('[12:00:01] [Server thread/INFO]: <bob> new log\n')
        self.collector.collect_log(self.log)
        self.assertEqual(self.documents('server_log_gap')[0]['reason'],'rotated_inode_tail_truncated_or_rewritten')

    def test_invalid_source_clock_and_bounded_parse_are_truthfully_incomplete(self):
        raw=b'[99:99:99] [Server thread/INFO]: alice died\n[12:00:00] [Server thread/INFO]: bob died\n'
        result=normalize_segment(raw,cursor={},path='x',generation='g',offset=0,world_epoch=0,mtime=self.mtime)
        self.assertFalse(result['complete']);self.assertEqual(result['invalid_source_times'],1)
        self.assertEqual(result['events'][0]['actor_id'],'bob')
        result=normalize_segment(raw[raw.index(b'[12:'):]*4,cursor={},path='x',generation='g',offset=0,world_epoch=0,mtime=self.mtime,max_entries=2)
        self.assertEqual(result['events_omitted'],2);self.assertFalse(result['complete'])

    def test_unarchived_tail_is_persisted_and_reported_before_final_fragment_capture(self):
        self.log.write_text('[12:00:00] [Server thread/INFO]: <alice> complete\npartial trailing bytes')
        self.collector.collect_log(self.log)
        self.assertEqual(self.collector.log_status(self.log)['pending_bytes'],len(b'partial trailing bytes'))
        report=build_report(self.run)
        self.assertFalse(report['paper_metrics']['full_session']['server_evidence_complete'])
        self.assertEqual(report['paper_metrics']['full_session']['incomplete']['unarchived_server_log_bytes'],22)
        self.collector.collect_log(self.log,final=True)
        self.assertEqual(self.collector.log_status(self.log)['pending_bytes'],0)
        last=self.documents()[-1]
        self.assertEqual(artifact_bytes(self.run,last['raw_sha256']),b'partial trailing bytes')
        self.assertFalse(last['complete'])


class FinalSupervisorCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_drain_repeats_bounded_passes_without_splitting_normal_lines(self):
        from types import SimpleNamespace
        from minecraft_harness.supervisor import Supervisor
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);log=root/'latest.log';run=root/'run'
            log.write_text(''.join(f'[12:00:00] [Server thread/INFO]: <alice> {index}\n' for index in range(25)))
            recorder=Recorder(run)
            try:
                collector=ServerEvidenceCollector(run,recorder)
                supervisor=Supervisor({'server':{'log_path':str(log)}},run)
                supervisor.recorder=recorder;supervisor.gateway=SimpleNamespace(world_epoch=0)
                with patch('minecraft_harness.evaluation.collector.LOG_COLLECTION_BYTES',200):
                    collector.collect_log(log)
                    self.assertGreater(collector.log_status(log)['pending_bytes'],0)
                    await supervisor._collect_logs(collector,final=True)
                self.assertEqual(collector.log_status(log)['pending_bytes'],0)
                documents=[json.loads(artifact_bytes(run,row['artifact_sha256'])) for row in registered_evidence(run)]
                self.assertGreater(len(documents),1)
                self.assertEqual(sum(len(document['events']) for document in documents),25)
                self.assertTrue(all(document['complete'] for document in documents))
            finally:recorder.close()

    async def test_final_drain_has_total_control_deadline(self):
        import time
        from types import SimpleNamespace
        from minecraft_harness.supervisor import Supervisor
        class NeverCaughtUp:
            def collect_log(self,*args,**kwargs):time.sleep(.005)
            def log_status(self,*args):return {'pending_bytes':1}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);log=root/'latest.log';log.write_text('still writing')
            supervisor=Supervisor({'server':{'log_path':str(log),'control_timeout_s':.001}},root/'run')
            supervisor.gateway=SimpleNamespace(world_epoch=0)
            with self.assertRaisesRegex(RuntimeError,'deadline exceeded'):
                await supervisor._collect_logs(NeverCaughtUp(),final=True)


class PaperReportTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.run=self.root/'run'
        self.recorder=Recorder(self.run);self.addCleanup(self.recorder.close)
        self.base=datetime(2026,10,21,12,tzinfo=timezone.utc).timestamp()
        config={'experiment':{'task':'observe_only'},'agents':{'ids':['alice','bob']},'budgets':{'prices_per_million':RATES}}
        (self.run/'manifest.json').write_text(json.dumps({'config':config}))

    def at(self,seconds):return datetime.fromtimestamp(self.base+seconds,timezone.utc).isoformat()

    def event(self,kind,payload=None,second=0,actor='supervisor',identity=None,source='harness'):
        event=self.recorder.append(kind,payload or {},actor_id=actor,execution_id=identity,source=source)
        with self.recorder.db:self.recorder.db.execute('UPDATE events SET received_at=? WHERE event_seq=?',(self.at(second),event['event_seq']))
        return event['event_seq']

    def program(self,actor,identity,start,end,status,digest):
        self.event('execution_accepted',{'source_ref':{'sha256':digest},'request_id':'r-'+identity},start-2,actor,identity)
        self.event('execution_running',{'started_at':self.at(start)},start,actor,identity)
        self.event('execution_'+status,{'started_at':self.at(start),'finished_at':self.at(end),'status':status,'cleanup':None},end,actor,identity)

    def model(self,identity,actor,role,purpose,start,end,raw):
        self.event('model_admitted',{'reservation_id':identity,'role':role,'purpose':purpose},start,actor)
        self.event('model_response',{'reservation_id':identity,'role':role,'purpose':purpose,'usage':raw,'latency_ms':2000},end,actor)
        self.event('model_usage',{'reservation_id':identity,'role':role,'purpose':purpose,'normalized_usage':normalize_usage(raw)},end,actor)

    def test_windows_start_at_real_execution_and_costs_roles_reuse_and_deaths_are_deduplicated(self):
        a=self.recorder.put_artifact('const sharedChestPosition = { x: 1, y: 64, z: 2 };\nawait bot.chat("first");')['sha256']
        b=self.recorder.put_artifact('const sharedChestPosition = { x: 1, y: 64, z: 2 };\nawait bot.chat("second");')['sha256']
        self.event('run_started',second=0)
        self.program('alice','a',30,40,'succeeded',a)
        self.program('bob','b',32,42,'timed_out',a)
        self.event('job_started',{'job':{'job_id':'j','started_at':self.at(36),'status':'running'}},36,'alice')
        self.event('job_succeeded',{'job':{'job_id':'j','started_at':self.at(36),'finished_at':self.at(48),'status':'succeeded'}},48,'alice')
        self.event('tool_result',{'tool':'memory_read'},35,'alice')
        self.event('tool_result',{'tool':'memory_write'},65,'alice')
        success=self.event('server_verified_success',second=45)
        self.recorder.set_state('first_success',{'first_success_event_seq':success})
        self.program('alice','c',60,70,'failed',b)
        self.model('c1','alice','player','agent_decision',10,43,usage())
        self.model('c2','observer','observer','observer_review',44,46,usage(10,5,0,0))
        self.model('c3','alice','player','context_compaction',60,67,usage(20,10,0,0))
        log=self.root/'latest.log'
        log.write_text('[12:00:36] [Server thread/INFO]: <alice> help careful chest lesson x=1 y=64 z=2\n'
                       '[12:00:38] [Server thread/INFO]: alice tried to swim in lava\n'
                       '[12:01:05] [Server thread/INFO]: alice fell from a high place\n')
        os.utime(log,(self.base+70,self.base+70))
        ServerEvidenceCollector(self.run,self.recorder).collect_log(log)
        for _ in range(4):self.event('bot_death',second=39,actor='alice',source='bot_runtime')
        self.event('run_finished',{'status':'stopped'},80)
        report=build_report(self.run)
        paper=report['paper_metrics'];full=paper['full_session'];before=paper['pre_first_success']
        self.assertEqual(full['duration_s'],50);self.assertEqual(before['duration_s'],15)
        self.assertEqual(report['metrics']['first_success_elapsed_s'],15)
        self.assertEqual(report['metrics']['wall_time_s'],50)
        self.assertEqual(report['metrics']['death_count'],2)
        alice={row['actor_id']:row for row in full['players']}['alice']
        early={row['actor_id']:row for row in before['players']}['alice']
        self.assertEqual(alice['program_duration_s']['known_count'],2)
        self.assertEqual(alice['program_duration_s']['p95'],10)
        self.assertEqual(alice['execution_status_counts'],{'failed':1,'succeeded':1})
        self.assertEqual(alice['death_causes'],{'lava':1,'fall':1})
        self.assertEqual(early['death_causes'],{'lava':1})
        self.assertAlmostEqual(alice['active_fraction'],.4)
        self.assertEqual(alice['combined_active_s'],28)
        self.assertEqual(early['combined_active_s'],15)
        self.assertEqual(early['memory_tool_calls'],{'memory_read':1})
        self.assertEqual(alice['memory_tool_calls'],{'memory_read':1,'memory_write':1})
        self.assertEqual(alice['chat_labels']['shared_storage'],1)
        groups=full['costs']['groups'];self.assertEqual(sum(row['calls'] for row in groups),3)
        self.assertEqual({row['purpose'] for row in groups},{'agent_decision','observer_review','context_compaction'})
        self.assertEqual(sum(row['calls'] for row in before['costs']['groups']),2)
        self.assertAlmostEqual(report['metrics']['estimated_cost_usd'],.00044)
        self.assertEqual(report['metrics']['agent_model_wait_sum_s'],6)
        self.assertEqual(paper['reuse']['repeated_execution_count'],1)
        self.assertEqual(paper['reuse']['cross_agent_program_hashes'],1)
        self.assertEqual(paper['reuse']['line_hashes_in_multiple_programs'],1)
        self.assertEqual(before['reuse']['line_hashes_in_multiple_programs'],0)
        paths=write_report(self.run)
        markdown=Path(paths['markdown']).read_text()
        self.assertIn('Before first verified success',markdown)
        self.assertIn('context_compaction',markdown)

    def test_cancelled_null_cleanup_and_output_truncation_are_reportable(self):
        self.event('execution_cancelled',{'cleanup':None,'started_at':self.at(1),'finished_at':self.at(2)},2,'alice','cancelled')
        self.event('execution_succeeded',{'cleanup':{'logs_suppressed':3},'evidence_incomplete':True,'started_at':self.at(3),'finished_at':self.at(4)},4,'alice','truncated')
        report=build_report(self.run)
        self.assertEqual(report['paper_metrics']['full_session']['incomplete']['execution_output_truncated'],1)
        self.assertTrue(write_report(self.run))

    def test_stage_completion_is_evidence_only_with_conservative_upper_bound(self):
        world=self.root/'world';adv=world/'advancements';adv.mkdir(parents=True)
        import hashlib,uuid
        player=str(uuid.UUID(bytes=hashlib.md5(b'OfflinePlayer:alice').digest(),version=3))
        path=adv/(player+'.json')
        path.write_text(json.dumps({'minecraft:story/root':{'done':True}}))
        collector=ServerEvidenceCollector(self.run,self.recorder)
        collector.collect_world(world,baseline=True)
        self.event('execution_running',{'started_at':self.at(30)},30,'alice','a')
        path.write_text(json.dumps({'minecraft:story/root':{'done':True},'minecraft:story/smelt_iron':{
            'done':True,'criteria':{'a':self.at(33),'b':self.at(34)}}}))
        collector.collect_world(world)
        report=build_report(self.run);stages=report['paper_metrics']['stage_timeline']
        self.assertEqual([stage['stage'] for stage in stages],['minecraft:story/smelt_iron'])
        self.assertEqual(stages[0]['actor_id'],'alice')
        self.assertEqual(stages[0]['source_time'],self.base+34)
        self.assertEqual(stages[0]['evidence_ref']['time_basis'],'last_recorded_criterion_upper_bound')

    def test_unknown_server_data_and_usage_remain_null_with_known_partial_sums(self):
        self.model('a','alice','player','agent_decision',1,2,usage())
        self.model('b','alice','player','agent_decision',3,4,{'input_tokens':10,'output_tokens':5})
        self.event('execution_interrupted',{'cleanup':None},5,'alice','a')
        report=build_report(self.run);window=report['paper_metrics']['full_session']
        self.assertIsNone(window['duration_s']);self.assertIsNone(report['metrics']['death_count'])
        group=window['costs']['groups'][0]
        self.assertIsNone(group['cached_input_tokens']);self.assertIsNone(group['estimated_cost_usd'])
        self.assertEqual(group['known_partial_sums']['cached_input_tokens'],40)
        self.assertIsNone(report['paper_metrics']['pre_first_success'])
        player={row['actor_id']:row for row in window['players']}['alice']
        self.assertEqual(player['infrastructure_interruption_count'],1)
        self.assertIsNone(player['death_count'])
