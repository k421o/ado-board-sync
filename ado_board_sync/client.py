"""Small REST adapter; credentials stay in memory, errors never echo bodies."""

import base64
import json
import os
import re
import subprocess
import sys
import time
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class SyncError(Exception):
    """An error safe to display to the command's caller."""


def segment(value):
    return quote(str(value), safe='')


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials(mode, organization):
    if mode == 'keychain':
        if sys.platform != 'darwin':
            raise SyncError('Keychain authentication requires macOS.')
        try:
            result = subprocess.run(
                ['/usr/bin/security', 'find-generic-password', '-s', 'ado-board-sync',
                 '-a', organization, '-w'], capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SyncError('Keychain lookup failed or timed out.') from None
        if result.returncode:
            raise SyncError('No accessible Keychain password: service ado-board-sync, '
                            'account equal to the organization.')
        try:
            pat = result.stdout.decode('utf-8').strip()
        except UnicodeError:
            raise SyncError('Keychain password is not valid UTF-8.') from None
    else:
        pat = os.environ.get('ADO_PAT') or os.environ.get('AZURE_DEVOPS_EXT_PAT', '')
    if not pat or '\n' in pat or '\r' in pat:
        raise SyncError('Set ADO_PAT or AZURE_DEVOPS_EXT_PAT in memory, or use --auth keychain.')
    return pat


class Client:
    def __init__(self, organization, project, pat, base_url=None, timeout=30):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', organization):
            raise SyncError('Organization must be its short name, not a URL.')
        if not project.strip() or any(ord(c) < 32 for c in project):
            raise SyncError('Project must be a nonempty name or ID.')
        if base_url:
            parsed = urlsplit(base_url)
            if (parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {'', '/'}):
                raise SyncError('--base-url permits only an HTTP localhost test server.')
            self.root = base_url.rstrip('/') + '/' + segment(organization)
        else:
            self.root = 'https://dev.azure.com/' + segment(organization)
        self.organization, self.project = organization, project
        self.project_route = '/' + segment(project)
        self.timeout = timeout
        self._pat = pat
        self._encoded = base64.b64encode((':' + pat).encode()).decode()
        handlers = [NoRedirect()]
        if base_url:
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)

    def request(self, method, route, body=None, *, params=None, patch=False, retry=True):
        url = self.root + route + '?' + urlencode({'api-version': '7.1', **(params or {})})
        data = None if body is None else json.dumps(body).encode('utf-8')
        for attempt in range(3):
            req = Request(url, data=data, method=method, headers={
                'Authorization': 'Basic ' + self._encoded,
                'Accept': 'application/json',
                'Content-Type': 'application/json-patch+json' if patch else 'application/json',
            })
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    # An unexpected continuation means we cannot prove completeness.
                    if response.headers.get('x-ms-continuationtoken'):
                        raise SyncError('Unexpected paginated response; previous snapshot retained.')
                    raw = response.read().decode('utf-8')
                # Guard against an error/proxy response reflecting credentials into saved data.
                raw = raw.replace(self._encoded, '[redacted]').replace(self._pat, '[redacted]')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise SyncError('ADO returned an unexpected JSON envelope.')
                return value
            except HTTPError as error:
                code, header = error.code, error.headers.get('Retry-After')
                error.close()
                if retry and code in {429, 502, 503, 504} and attempt < 2:
                    time.sleep(retry_delay(header, attempt))
                    continue
                hint = (' Check token scopes and project access.' if code in {401, 403}
                        else ' Check project/team/board names.' if code == 404
                        else ' Redirects are refused; check the organization.' if 300 <= code < 400
                        else '')
                raise SyncError(f'ADO HTTP {code}.{hint}') from None
            except (URLError, TimeoutError, ConnectionError, OSError, HTTPException):
                if retry and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise SyncError('ADO connection failed. Previous local snapshot is unchanged; '
                                'for seed, rerun the same command to reconcile possible writes.') from None
            except (ValueError, UnicodeError):
                raise SyncError('ADO returned invalid JSON; check authentication and endpoint.') from None
        raise SyncError('ADO retries exhausted.')

    def query(self, query, top=1000):
        return self.request('POST', self.project_route + '/_apis/wit/wiql',
                            {'query': query}, params={'$top': top, 'timePrecision': 'true'})

    def hydrate(self, ids, as_of=None):
        body = {'ids': ids, '$expand': 'all', 'errorPolicy': 'fail'}
        if as_of:
            body['asOf'] = as_of
        data = self.request('POST', self.project_route + '/_apis/wit/workitemsbatch', body)
        items = data.get('value')
        if (not isinstance(items, list) or len(items) != len(ids)
                or any(not isinstance(item, dict) or type(item.get('id')) is not int
                       for item in items)
                or {item['id'] for item in items} != set(ids)):
            raise SyncError('Incomplete work item batch; refusing partial results.')
        return items


def retry_delay(header, attempt):
    try:
        delay = float(header)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(header).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            delay = 2 ** attempt
    return min(30, max(0, delay))
