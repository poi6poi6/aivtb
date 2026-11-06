"""One owner-command entry point, shared by both task frontends."""


async def task_action(manager, data):
    if not isinstance(data, dict):
        raise ValueError('请求格式错误')
    action = data.get('action')
    key = data.get('id')
    if isinstance(action, str) and action.startswith('self_upgrade_'):
        return await manager.self_upgrade.action(action, data)
    if action == 'submit_commission':
        return manager.submit_commission(data.get('prompt'), data.get('key'))
    if action == 'submit':
        return manager.submit(data.get('prompt'), data.get('kind'), owner=True,
            resource_profile=data.get('resource_profile', 'standard'),
            execution_mode=data.get('execution_mode', 'auto'))
    if action == 'cancel':
        await manager.cancel(key)
        return {'ok': True}
    if action == 'approve':
        manager.approve(key)
        return {'ok': True}
    if action == 'answer':
        manager.answer(key, data.get('accept') is True)
        return {'ok': True}
    if action == 'check':
        return await manager.check()
    if action == 'review_code':
        return manager.code_review(key).public()
    if action == 'apply_code':
        return manager.apply_code(key, data.get('token'))
    if action == 'verify_code':
        return await manager.verify_code(key)
    if action == 'rollback_code':
        return manager.rollback_code(key)
    if action == 'amend':
        return await manager.amend(key, data.get('instruction'), token=data.get('token'))
    if action == 'finish_drawing':
        return await manager.finish_drawing(key, data.get('instruction', ''), token=data.get('token'))
    if action in {'drawing_state', 'drawing_states', 'drawing_playback'}:
        from .drawing_session import DrawingSessions
        if not hasattr(manager, 'drawing_sessions'):
            manager.drawing_sessions = DrawingSessions(manager)
        if action == 'drawing_states':
            ids = data.get('ids')
            if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) for i in ids):
                raise ValueError('绘画列表无效')
            result = {}
            for job_id in ids:
                try:
                    result[job_id] = manager.drawing_sessions.state(job_id)
                except (ValueError, OSError, StopIteration, KeyError):
                    result[job_id] = {'error': '绘画状态未确认'}
            return result
        if action == 'drawing_state':
            return manager.drawing_sessions.state(key)
        return manager.drawing_sessions.control(key, data)
    if action == 'accept_code':
        return await manager.accept_code(key, data.get('scenario'), data.get('expected'),
                                         data.get('observed'), data.get('passed'))
    raise ValueError('未知操作')
