"""Authenticated streaming access to an operator-deployed private R2 Worker."""
import hashlib
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class WorkerStore:
    packed = True
    chunk_size = 64 * 1024 * 1024
    compress_payload = True
    parallel_uploads = True

    def __init__(self, url, token_env='OUTPOST_ARCHIVE_TOKEN'):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Archive Worker requires an HTTPS origin without credentials or a path')
        if not re.fullmatch('[A-Z][A-Z0-9_]*', token_env):
            raise ValueError('Invalid archive credential environment reference')
        self.url = url.rstrip('/')
        self.token_env = token_env
        self._opener = build_opener(NoRedirect())

    def _request(self, key, data=None):
        if not re.fullmatch(r'apps/[a-z][a-z0-9-]{0,63}/snapshots/[0-9a-f]{64}/(?:COMMITTED\.json|manifest\.json|transport\.json|payload/[0-9]{6})', key):
            raise ValueError('Archive access requires a packed snapshot object path')
        token = os.environ.get(self.token_env)
        if not token:
            raise ValueError('Set the configured archive token environment variable')
        headers = {'Authorization': 'Bearer ' + token, 'User-Agent': 'Outpost archive/1.0'}
        if data is not None:
            if len(data) > 80 * 1024 * 1024:
                raise ValueError('Archive upload exceeds the bounded object size')
            headers.update({'Content-Type': 'application/octet-stream',
                            'X-Outpost-SHA256': hashlib.sha256(data).hexdigest()})
        request = Request(self.url + '/api/archive/' + key[5:], data=data,
                          headers=headers, method='PUT' if data is not None else 'GET')
        try:
            return self._opener.open(request, timeout=180)
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 404:
                raise FileNotFoundError('Archived object is absent') from None
            raise RuntimeError('Archive request failed (HTTP ' + str(status) + '); completion is unconfirmed') from None
        except URLError:
            raise RuntimeError('Archive connection failed; completion is unconfirmed') from None

    def iter_object(self, key):
        with self._request(key) as response:
            while block := response.read(1024 * 1024):
                yield block

    def hash_object(self, key):
        value = hashlib.sha256()
        try:
            for block in self.iter_object(key):
                value.update(block)
        except FileNotFoundError:
            return None
        return value.hexdigest()

    def read_payload(self, key):
        data = bytearray()
        for block in self.iter_object(key):
            data.extend(block)
            if len(data) > 129 * 1024 * 1024:
                raise ValueError('Archived payload exceeds the bounded size')
        return data

    def get(self, key, destination):
        # Open the response first so a missing object never leaves an empty file.
        try:
            response = self._request(key)
        except FileNotFoundError:
            return False
        with response, Path(destination).open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        return True

    def put_payload(self, key, data):
        with self._request(key, data) as response:
            response.read(65536)

    def put(self, key, path):
        if Path(path).stat().st_size > 80 * 1024 * 1024:
            raise ValueError('Archive metadata exceeds the bounded size')
        self.put_payload(key, Path(path).read_bytes())
