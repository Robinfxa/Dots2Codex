#!/usr/bin/env python3
"""Bounded anonymous read-only access to exactly Robinfxa/Dots2Codex.
No subprocess, local repository reads, credentials, writes, or redirect following.
"""
import hashlib
import json
import re
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = 'Robinfxa/Dots2Codex'
CONTRACT = 'dots-public-repo-fetch/1'
API = 'https://api.github.com/repos/' + REPO
RAW = 'https://raw.githubusercontent.com/' + REPO
MAX_BODY = 262144
MAX_STDOUT = 12000
MAX_TEXT = 8500
MAX_TREE = 80

class FetchError(Exception):
    pass


def safe_path(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', value) or len(value) > 180:
        raise FetchError('invalid_path')
    if any(p in ('.', '..') or p.startswith('.') and p not in ('.gitignore', '.gitattributes') for p in value.split('/')):
        raise FetchError('invalid_path')
    return value


def parse(argv):
    if argv == ['tree']:
        return {'action': 'tree'}
    if len(argv) not in (4, 5) or argv[0] not in ('read', 'search') or not re.fullmatch('[0-9a-f]{40}', argv[1]):
        raise FetchError('invalid_arguments')
    out = {'action': argv[0], 'commit': argv[1], 'path': safe_path(argv[2])}
    if argv[0] == 'read':
        if len(argv) != 5 or not re.fullmatch('[1-9][0-9]{0,5}', argv[3]) or not re.fullmatch('[1-9][0-9]{0,2}', argv[4]) or int(argv[4]) > 150:
            raise FetchError('invalid_line_range')
        out.update(start=int(argv[3]), count=int(argv[4]))
    else:
        if len(argv) != 4 or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', argv[3]):
            raise FetchError('invalid_search_token')
        out['term'] = argv[3]
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchError('redirect_denied')


def fetch(url):
    # URL construction is internal, and neither credentials nor proxy environment is used.
    if not (url.startswith(API + '/') or url == API or url.startswith(RAW + '/')):
        raise FetchError('destination_denied')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(url, headers={'User-Agent': 'Dots2Codex-ReadOnly-Review', 'Accept': 'application/vnd.github+json'})
    try:
        with opener.open(req, timeout=5) as response:
            if response.status != 200 or response.url != url:
                raise FetchError('unexpected_response')
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise FetchError('response_too_large')
            return data
    except urllib.error.HTTPError as e:
        raise FetchError('http_' + str(e.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FetchError('network_unavailable') from None


def strict_json(data):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise FetchError('duplicate_json_key')
            d[k] = v
        return d
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(FetchError('non_finite_json')))
    except (ValueError, UnicodeError):
        raise FetchError('invalid_json') from None


def clipped(text, limit=MAX_TEXT):
    data = text.encode('utf-8')
    return data[:limit].decode('utf-8', errors='ignore'), len(data) > limit


def run(action):
    if action['action'] == 'tree':
        meta = strict_json(fetch(API))
        branch = meta.get('default_branch')
        if not isinstance(branch, str) or not branch or len(branch) > 200 or meta.get('full_name') != REPO or meta.get('private') is not False:
            raise FetchError('unexpected_repository')
        ref = strict_json(fetch(API + '/commits/' + urllib.parse.quote(branch, safe='')))
        sha = ref.get('sha')
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
            raise FetchError('invalid_commit')
        tree = strict_json(fetch(API + '/git/trees/' + sha + '?recursive=1'))
        entries = tree.get('tree')
        if not isinstance(entries, list) or tree.get('truncated') is True:
            raise FetchError('tree_incomplete')
        paths = []
        for e in entries:
            if e.get('type') != 'blob' or e.get('mode') not in ('100644', '100755'):
                continue
            try:
                path = safe_path(e['path'])
            except (FetchError, KeyError):
                continue
            if type(e.get('size')) is int and e['size'] <= MAX_BODY:
                paths.append({'path': path, 'bytes': e['size'], 'mode': e['mode']})
        paths.sort(key=lambda e: (e['path'].count('/'), e['path']))
        return dict(commit=sha, branch=branch, description=str(meta.get('description') or '')[:400],
                    files=paths[:MAX_TREE], eligible_files=len(paths), truncated=len(paths) > MAX_TREE,
                    url='https://github.com/' + REPO + '/tree/' + sha)
    url = RAW + '/' + action['commit'] + '/' + action['path']
    data = fetch(url)
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise FetchError('non_utf8_file') from None
    if '\x00' in text:
        raise FetchError('binary_file_denied')
    lines = text.splitlines()
    if action['action'] == 'read':
        selected = list(enumerate(lines, 1))[action['start'] - 1:action['start'] - 1 + action['count']]
    else:
        selected = [(i, line) for i, line in enumerate(lines, 1) if action['term'].lower() in line.lower()]
    rendered, byte_truncated = clipped('\n'.join(str(i) + ': ' + line for i, line in selected[:150]))
    return dict(commit=action['commit'], path=action['path'], file_sha256=hashlib.sha256(data).hexdigest(),
                file_bytes=len(data), total_lines=len(lines), matched_lines=len(selected), text=rendered,
                truncated=byte_truncated or len(selected) > 150,
                url='https://github.com/' + REPO + '/blob/' + action['commit'] + '/' + action['path'])


def main(argv=None):
    action = {}
    try:
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(FetchError('total_timeout')))
        signal.alarm(22)
        action = parse(sys.argv[1:] if argv is None else argv)
        out = dict(contract=CONTRACT, repo=REPO, ok=True, request=action, **run(action))
        # Preserve valid complete JSON; never slice JSON or silently truncate stdout.
        while len(json.dumps(out, ensure_ascii=False).encode()) > MAX_STDOUT and out.get('files'):
            out['files'].pop(); out['truncated'] = True
        if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_STDOUT:
            raise FetchError('output_limit')
        code = 0
    except (FetchError, KeyError, TypeError) as e:
        out = dict(contract=CONTRACT, repo=REPO, ok=False, request=action, error=str(e) if isinstance(e, FetchError) else 'unexpected_data')
        code = 1
    finally:
        signal.alarm(0)
    print(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
