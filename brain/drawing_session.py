"""One host-owned drawing timeline shared by the console and broadcast pages."""
from collections import OrderedDict
import hashlib
import json
import math
import secrets
import time

from .drawing import load_drawing


class DrawingSessions:
    def __init__(self, manager, clock=time.monotonic):
        self.manager, self.clock = manager, clock
        self.sessions = OrderedDict()
        self.cache = OrderedDict()

    def resolve(self, job_id):
        job = self.manager.get(job_id)
        if job['kind'] != 'drawing':
            raise ValueError('此任务不是逐笔绘画')
        root = job.get('drawing_root', job['id'])
        job = self.manager.get(root)
        ready = None
        seen = set()
        while True:
            if job['id'] in seen or len(seen) >= 80 or job['kind'] != 'drawing':
                raise ValueError('绘画修订关系无效')
            seen.add(job['id'])
            if job['state'] == 'succeeded' and 'drawing.json' in job.get('artifacts', []):
                ready = job
            if not job.get('superseded_by'):
                return root, job, ready
            job = self.manager.get(job['superseded_by'])

    def document(self, job):
        from .agent_tasks import artifact_path
        path = artifact_path(self.manager.folder / job['id'], 'drawing.json')
        stat = path.stat()
        key = (job['id'], stat.st_mtime_ns, stat.st_size)
        cached = self.cache.get(key)
        if cached is None:
            data = load_drawing(path)
            version = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]
            cached = (data, version, sum(s['duration'] for s in data['strokes']))
            self.cache[key] = cached
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
        return cached

    def state(self, job_id):
        root, latest, ready = self.resolve(job_id)
        result = {'root': root, 'latest': {k: latest.get(k) for k in ('id', 'state', 'detail')},
                  'drawing': None, 'playback': None}
        if ready is None:
            return result
        data, version, duration = self.document(ready)
        session = self.sessions.get(root)
        if session is None or session['version'] != version or session['job_id'] != ready['id']:
            session = {'session': secrets.token_hex(12), 'revision': 0, 'position': 0.0,
                       'playing': False, 'speed': 1.0, 'anchor': self.clock(),
                       'version': version, 'job_id': ready['id']}
            self.sessions[root] = session
            while len(self.sessions) > 64:
                self.sessions.popitem(last=False)
        self.sessions.move_to_end(root)
        position = self.position(session, duration)
        result['drawing'] = {'job_id': ready['id'], 'version': version, 'duration': duration,
                             'stage': ready.get('drawing_stage', 'final'), 'title': data['title']}
        result['playback'] = {k: session[k] for k in ('session', 'revision', 'speed')}
        result['playback'].update(position=position, playing=session['playing'] and position < duration)
        return result

    def position(self, session, duration):
        elapsed = max(0, self.clock() - session['anchor']) * 1000 * session['speed'] if session['playing'] else 0
        return min(duration, session['position'] + elapsed)

    def control(self, job_id, data):
        state = self.state(job_id)
        if state['drawing'] is None:
            raise ValueError('笔画尚未准备好')
        session = self.sessions[state['root']]
        if any(data.get(k) != session[k] for k in ('session', 'revision', 'version')):
            raise ValueError('画布版本或播放状态已变化，请同步后重试')
        operation = data.get('operation')
        if operation not in {'play', 'pause', 'restart', 'speed'}:
            raise ValueError('未知的画布操作')
        speed = data.get('speed', session['speed'])
        if type(speed) not in (int, float) or not math.isfinite(speed) or not .5 <= speed <= 4:
            raise ValueError('画布速度须在 0.5～4 倍之间')
        duration = state['drawing']['duration']
        session['position'] = self.position(session, duration)
        if operation == 'restart' or operation == 'play' and session['position'] >= duration:
            session['position'] = 0.0
        if operation in {'pause', 'restart'}:
            session['playing'] = False
        elif operation == 'play':
            session['playing'] = True
        session['speed'] = speed
        session['anchor'] = self.clock()
        session['revision'] += 1
        return self.state(job_id)
