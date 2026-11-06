"""Check a running code revision without treating unavailability as a rollback request."""
import re
import secrets
import time

import httpx

from .code_review import digest, sources


def _manifest(value):
    return (isinstance(value, dict) and bool(value) and
            all(isinstance(name, str) and isinstance(version, str) and
                re.fullmatch(r'[0-9a-f]{64}', version)
                for name, version in value.items()))


def _differences(expected, actual):
    """Only names and counts are needed; do not flood the console with source hashes."""
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(name for name in expected.keys() & actual.keys()
                     if expected[name] != actual[name])
    return {'missing': missing[:20], 'extra': extra[:20], 'changed': changed[:20],
            'total': len(missing) + len(extra) + len(changed)}


def _difference_label(label, difference):
    names = (difference['changed'] + difference['missing'] + difference['extra'])[:6]
    return f"{label} {difference['total']} 处：" + '、'.join(names)


async def verify_code(manager, job_id):
    job = manager.get(job_id)
    if job['state'] != 'applied':
        raise ValueError('只有已应用、待验证的任务可以验证')
    review = manager.code_review(job_id)
    revision = job.get('revision')
    attempts = getattr(manager, '_code_verification_attempts', None)
    if attempts is None:
        attempts = manager._code_verification_attempts = {}
    attempt = attempts[job_id] = secrets.token_hex(8)
    manager.update(job, verification_check={'status': 'checking', 'at': time.time()})

    def still_current():
        current = manager.get(job_id)
        if (current is not job or current['state'] != 'applied' or
                current.get('revision') != revision or attempts.get(job_id) != attempt):
            raise ValueError('验证期间任务状态或检查批次发生变化，请查看当前状态后重试')

    def unsuccessful(reason, detail, *, retryable=True, restart_required=False,
                     status='pending', **extra):
        still_current()
        check = {'status': status, 'reason': reason, 'detail': detail,
                 'retryable': retryable, 'restart_required': restart_required,
                 'at': time.time(), **extra}
        manager.update(job, detail=detail, verification_check=check)
        return {'ok': True, 'verified': False, **check}

    try:
        expected = review.read().get('source_version')
    except (OSError, ValueError, AttributeError):
        expected = None
    if not _manifest(expected):
        return unsuccessful('review_invalid', '审核记录缺少有效的完整源码版本；文件已保留，请重新审核并测试',
                            retryable=False, status='blocked')

    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(manager.verification_url)
            response.raise_for_status()
            health = response.json()
    except httpx.TimeoutException:
        return unsuccessful('timeout', '主服务健康检查超时；文件已保留，等待启动完成后可重试')
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        return unsuccessful('http_error', f'主服务健康检查返回 HTTP {status_code}；文件已保留，请检查服务状态后重试',
                            http_status=status_code)
    except httpx.HTTPError:
        return unsuccessful('unavailable', '暂时无法连接主服务；文件已保留，请启动服务或等待重启完成后重试')
    except ValueError:
        return unsuccessful('malformed_health', '主服务健康检查未返回有效 JSON；文件已保留，请检查服务地址及启动日志')

    # A rollback, another check or a revision change during the request wins.
    still_current()
    if not isinstance(health, dict):
        return unsuccessful('malformed_health', '主服务健康检查格式无效；文件已保留，请检查服务地址及启动日志')
    if health.get('scope') != 'main':
        return unsuccessful('wrong_scope', '连接到的不是主服务健康接口；文件已保留，请检查服务地址')
    if health.get('ready') is not True:
        return unsuccessful('not_ready', '主服务尚未就绪；文件已保留，等待启动完成后可重试')
    boot = health.get('boot_id')
    if not isinstance(boot, str) or not boot.strip() or len(boot) > 128:
        return unsuccessful('invalid_boot', '主服务未提供有效启动标识；文件已保留，不能确认新版本已启动')
    runtime = health.get('code')
    if not _manifest(runtime):
        return unsuccessful('malformed_manifest', '主服务未提供有效的完整源码版本；文件已保留，不能确认版本生效')
    try:
        disk = {name: digest(data) for name, data in sources(manager.root).items()}
    except (OSError, ValueError):
        return unsuccessful('source_unreadable', '当前源码无法完整读取；文件已保留，请检查文件后重新验证',
                            retryable=False, status='blocked')
    runtime_diff = _differences(expected, runtime)
    disk_diff = _differences(expected, disk)
    if runtime_diff['total'] or disk_diff['total']:
        details = []
        if runtime_diff['total']:
            details.append(_difference_label('运行版本与测试版本不一致', runtime_diff))
        if disk_diff['total']:
            details.append(_difference_label('当前文件与测试版本不一致', disk_diff))
        details.append('文件已保留；' + ('请重新审核并测试当前改动' if disk_diff['total'] else '请重启主服务后重试'))
        return unsuccessful('version_mismatch', '；'.join(details),
                            retryable=not bool(disk_diff['total']),
                            restart_required=bool(runtime_diff['total'] and not disk_diff['total']),
                            status='mismatch', runtime_diff=runtime_diff, disk_diff=disk_diff)

    still_current()
    detail = '已确认主服务按测试版本启动并通过健康检查；具体功能效果仍需体验验证'
    manager.update(job, state='verified', detail=detail, runtime_boot=boot,
                   verification_check={'status': 'verified', 'reason': 'verified',
                                       'boot_id': boot, 'at': time.time()})
    return {'ok': True, 'verified': True, 'boot_id': boot}
