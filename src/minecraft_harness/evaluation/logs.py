"""Conservative, versioned normalization of authoritative Minecraft log bytes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

LOG_VERSION = 'vanilla-java-en-log-v1'
CHAT_VERSION = 'chat-rules-v1'
LINE = re.compile(r'^\[(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?(?P<clock>\d{2}:\d{2}:\d{2})\] \[Server thread/(?:INFO|WARN|ERROR)\]: (?P<message>.*)$')
CHAT = re.compile(r'^(?:\[Not Secure\] )?<(?P<actor>\w{1,16})> (?P<text>.*)$')
ADVANCEMENT = re.compile(r'^(?P<actor>\w{1,16}) has (?P<verb>made the advancement|reached the goal|completed the challenge) \[(?P<title>.+)\]$')
DEATH = re.compile(r"^(?P<actor>\w{1,16}) (?P<detail>died|was (?:slain|shot|blown up|killed|squashed|squished|impaled|poked|struck|pricked|stung|pummeled|fireballed|obliterated|skewered|roasted|doomed to fall)|hit the ground|fell|drowned|went up in flames|burned to death|tried to swim in lava|starved to death|froze to death|suffocated|experienced kinetic energy|discovered the floor|walked into (?:fire|a cactus)|withered away|left the confines of this world|didn't want to live in the same world)(?=\s|$).*?$",re.I)
LABELS = {
    'warning': re.compile(r'\b(?:danger|careful|creeper|lava|watch out|warning)\b|危险|小心|警告',re.I),
    'rescue': re.compile(r'\b(?:help|rescue|save me|stuck|trapped)\b|救|卡住',re.I),
    'shared_storage': re.compile(r'\b(?:chest|barrel|storage)\b|箱子|仓库|储物',re.I),
    'lesson': re.compile(r'\b(?:learned|lesson|remember|next time|avoid|learn from)\b|教训|经验|记住|下次',re.I),
    'coordinates': re.compile(r'\b[xyz]\s*[=:]\s*-?\d+|[(-]?\d+\s*[,/]\s*-?\d+\s*[,/]\s*-?\d+|坐标',re.I),
}


def chat_labels(message):
    return [name for name, expression in LABELS.items() if expression.search(message)]


def death_cause(message):
    lowered=message.lower()
    for cause,words in (
        ('void',('out of the world','confines of this world','same world','into the void')),
        ('falling_object',('falling anvil','falling block','falling stalactite')),
        ('cramming',('squished too much',)),('cactus',('cactus','pricked to death')),
        ('sweet_berry_bush',('sweet berry bush',)),('sonic_boom',('sonically-charged shriek',)),
        ('dragon_breath',('dragon breath',)),
        ('hot_floor',('discovered the floor was lava',)),('kinetic_energy',('experienced kinetic energy',)),
        ('lava',('lava',)),('fall',('fell','hit the ground','kinetic energy')),
        ('drowning',('drowned',)),('fire',('flames','burned','fire')),
        ('starvation',('starved',)),('freezing',('froze',)),('suffocation',('suffocated',)),
        ('explosion',('blown up','intentional game design')),('lightning',('lightning',)),('magic',('magic','withered')),
        ('combat',('slain','shot','killed','impaled','poked','squashed','pummeled','stung','sonically'))):
        if any(word in lowered for word in words): return cause
    return 'other_or_unknown'


def normalize_segment(raw, *, cursor, path, generation, offset, world_epoch, mtime,
                      source_timezone='UTC', max_entries=4096):
    zone=ZoneInfo(source_timezone)
    received=datetime.now(timezone.utc).isoformat()
    mtime_local=datetime.fromtimestamp(mtime,zone)
    day=cursor.get('source_date')
    day=datetime.fromisoformat(day).date() if day else None
    last_clock=cursor.get('last_clock')
    entries=[]
    omitted=0; invalid_times=0; unmatched=0;invalid_utf8=0
    position=offset
    line_no=cursor.get('line',1)
    fragment=cursor.get('fragment',False)
    for index,line in enumerate(raw.splitlines(keepends=True)):
        start=position; position+=len(line)
        if (index==0 and fragment) or not line.endswith(b'\n'):
            line_no+=line.count(b'\n')
            continue
        try: text=line.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError:
            text=line.decode('utf-8',errors='replace').rstrip('\r\n');invalid_utf8+=1
        match=LINE.fullmatch(text)
        if match:
            clock=match['clock']; h,m,s=map(int,clock.split(':'))
            try:
                if not (0<=h<=23 and 0<=m<=59 and 0<=s<=59): raise ValueError('invalid source clock')
                explicit_day=datetime.fromisoformat(match['date']).date() if match['date'] else None
            except ValueError:
                invalid_times+=1; line_no+=line.count(b'\n'); continue
            seconds=h*3600+m*60+s
            uncertainty=[]
            if match['date']:
                day=explicit_day
                confidence='explicit_date'
            else:
                confidence='mtime_date_anchor_with_midnight_rollover'
                if day is None:
                    day=mtime_local.date()
                    if seconds-(mtime_local.hour*3600+mtime_local.minute*60+mtime_local.second)>43200:
                        day-=timedelta(days=1)
                if last_clock is not None and seconds < last_clock-43200:
                    day+=timedelta(days=1)
                elif last_clock is not None and seconds < last_clock:
                    uncertainty.append('source_clock_regressed')
            last_clock=seconds
            local=datetime(day.year,day.month,day.day,h,m,s,tzinfo=zone)
            if local.utcoffset()!=local.replace(fold=1).utcoffset():
                uncertainty.append('source_local_time_ambiguous_or_nonexistent')
            stamp=local.astimezone(timezone.utc).isoformat()
            message=match['message']
            item={'normalizer_version':LOG_VERSION,'source_time':stamp,'source_clock':clock,
                  'source_time_basis':confidence,'received_at':received,'world_epoch':world_epoch,
                  'source':{'path':path,'generation':generation,'offset_start':start,'offset_end':position,'line':line_no},
                  'time_warnings':uncertainty,'raw_message':message}
            chat=CHAT.fullmatch(message)
            advancement=ADVANCEMENT.fullmatch(message)
            death=DEATH.fullmatch(message)
            if chat:
                item.update(kind='chat',actor_id=chat['actor'],message=chat['text'],
                            classification={'version':CHAT_VERSION,'labels':chat_labels(chat['text']),
                                            'claim':'deterministic lexical labels; no intent or causality inference'})
            elif advancement:
                item.update(kind='advancement',actor_id=advancement['actor'],title=advancement['title'],
                            advancement_kind=advancement['verb'],canonical_id=None)
            elif death:
                item.update(kind='death',actor_id=death['actor'],cause=death_cause(message))
            else:
                item.update(kind='server_message',actor_id=None)
            if len(entries)<max_entries: entries.append(item)
            else: omitted+=1
        else:
            unmatched+=1
        line_no+=line.count(b'\n')
    return {'normalizer_version':LOG_VERSION,'events':entries,'events_omitted':omitted,
            'invalid_source_times':invalid_times,'unmatched_lines':unmatched,'invalid_utf8_lines':invalid_utf8,
            'complete':omitted==0 and invalid_times==0 and invalid_utf8==0 and not (raw and not raw.endswith(b'\n')) and not fragment,
            'semantic_coverage':'English Vanilla Java message grammar; unknown server messages retained without inferred death or advancement',
            'clock_after':{'source_date':day.isoformat() if day else None,'last_clock':last_clock},
            'source_timezone':source_timezone,'received_at':received}
