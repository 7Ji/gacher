#!/usr/bin/env python3

# gacher, a read-only git caching server
# Copyright (C) 2025-present Guoxin "7Ji" Pu

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import aiofiles
from aiohttp import web
import argparse
import asyncio
import dataclasses
import enum
import hashlib
import ipaddress
import json
import os
import re
import pathlib
import shutil
import textwrap
import time
import typing
from urllib.parse import urlparse
import xxhash

class WorkStatus(int, enum.Enum):
    OK = 0
    BAD = 1

async def run_async_check(program, *args, max_tries=1, **kwds) -> WorkStatus:
    for _ in range(max_tries):
        proc = await asyncio.create_subprocess_exec(
            program, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            **kwds
        )
        await proc.wait()
        if proc.returncode == 0:
            return WorkStatus.OK
    print(f"[gacher] child {program} {args} failed after {max_tries} tries")
    return WorkStatus.BAD

# outer caller should await
def prepare_to_run_async_with_pipe_out(program, *args, max_tries=1, **kwds):
    return asyncio.create_subprocess_exec(
        program, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        **kwds
    )

# outer caller should await
def prepare_to_run_async_with_pipe_in_out(program, *args, max_tries=1, **kwds):
    return asyncio.create_subprocess_exec(
        program, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        **kwds
    )

async def run_async_check_with_stdout(
    program, *args, max_tries=1, **kwds
) -> (WorkStatus, bytes):
    for _ in range(max_tries):
        proc = await prepare_to_run_async_with_pipe_out(
            program, *args,
            **kwds
        )
        await proc.wait()
        if proc.returncode == 0:
            return (WorkStatus.OK, await proc.stdout.read())
    print(f"[gacher] child {program} {args} failed after {max_tries} tries")
    return (WorkStatus.BAD, None)

def hash_str_to_str(content: str) -> str:
    return xxhash.xxh3_64_hexdigest(content)

def hash_str_to_bytes(content: str) -> bytes:
    return xxhash.xxh3_64_digest(content)

class RepoState(str, enum.Enum):
    UPDATING = "updating"
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    DEAD = "dead"

@dataclasses.dataclass
class RepoStat:
    state: RepoState
    lag: float
    idle: float
    hit: int

class RepoPaths:
    data: pathlib.Path # actual bare git repo, repos/data/[hash], determied len
    config: pathlib.Path # [git]/config, touched to record access time on disk
    link: pathlib.Path # human-readable link, undetermined depth
    relative_data: str
    relative_link: str
    relative_data_from_link: str

    # The upstream here already contains scheme
    def __init__(self, upstream: str, parent: pathlib.Path):

        self.relative_data = RepoPaths.calculate_relative_data(upstream)
        self.data = parent / self.relative_data
        self.config = self.data / 'config'

        self.relative_link = RepoPaths.calculate_relative_link(upstream)
        self.link = parent / self.relative_link

        self.relative_data_from_link = \
            "../" * self.relative_link.count('/') + self.relative_data

    @staticmethod
    def calculate_relative_data(upstream: str) -> str:
        return f"data/{hash_str_to_str(upstream)}"

    @staticmethod
    def calculate_relative_link(upstream: str) -> str:
        return f"links/{upstream.split('://', maxsplit=1)[1]}"

@dataclasses.dataclass
class RepoTimes:
    fetch: float = 0 # unix timestamp
    access: float = 0 # unix timestamp

@dataclasses.dataclass
class WorkerTimes:
    hot: int
    warm: int
    drop: int
    remove: int
    interval: int

class Repo:
    upstream: str
    paths: RepoPaths
    times: RepoTimes
    hits: int
    lock: asyncio.Lock

    def __init__(self, upstream: str, parent: pathlib.Path):
        self.upstream = upstream
        self.paths = RepoPaths(upstream, parent)
        self.times = RepoTimes()
        self.hits = 0
        self.lock = asyncio.Lock()
        print(f"[gacher] repo '{self.upstream}' ->'{self.paths.relative_data}'")

    async def ensure_exist(self):
        print(f"[gacher] ensuring local repo existence of '{self.upstream}'")
        if not self.paths.data.is_dir():
            print(f"[gacher] local repo for '{self.upstream}' does not exist, creating '{self.paths.data}'")
            self.paths.data.mkdir(parents=True)
            if await run_async_check('git', 'init', '--bare', self.paths.data) \
                or \
                await run_async_check(
                    'git', 'remote', 'add', '--mirror=fetch',
                    'origin', self.upstream,
                    cwd=self.paths.data
                ):
                raise Exception("failed to init local repo")
        self.paths.link.parent.mkdir(parents=True, exist_ok=True)
        if self.paths.link.exists():
            self.paths.link.unlink()
        self.paths.link.symlink_to(
            self.paths.relative_data_from_link,
            target_is_directory=False
        )

    def first_touch(self):
        self.times.access = self.paths.config.stat().st_mtime

    @classmethod
    async def new(cls, upstream: str, parent: pathlib.Path):
        repo = cls(upstream, parent)
        await repo.ensure_exist()
        repo.first_touch()
        return repo

    async def hit(self):
        async with self.lock:
            self.times.access = time.time()
            self.paths.config.touch()
            self.hits += 1

    def need_update(self, time_hot: float) -> bool:
        return time.time() - self.times.fetch > time_hot

    # this is only called in update(), lock was aquired there
    async def update_inner(self) -> WorkStatus:
        print(f"[gacher] updating '{self.upstream}'")
        # git complains if remote origin was added with --mirror without =fetch
        # or =push, so we added it with --mirror=fetch, that results in HEAD not
        # being updated during fetch, to fix it we set remote.origin.mirror=true
        # manually so fetch also updates HEAD
        if await run_async_check(
            'git',
                '-c', 'remote.origin.mirror=true',
                '-c', 'fetch.showForcedUpdates=false',
                '-c', 'advice.fetchShowForcedUpdates=false',
            'fetch',
                'origin', '+refs/*:refs/*',
            max_tries=3,
            cwd=self.paths.data
        ):
            print(f"[gacher] failed to upate '{self.upstream}'")
            return WorkStatus.BAD
        self.times.fetch = time.time()
        print(f"[gacher] updated '{self.upstream}'")
        return WorkStatus.OK

    async def update(self, time_hot: float) -> WorkStatus:
        if not self.need_update(time_hot):
            return WorkStatus.OK
        async with self.lock:
            if not self.need_update(time_hot):
                return WorkStatus.OK
            return await self.update_inner()

    def stat(self, times_worker: WorkerTimes) -> RepoStat:
        times = self.times
        locked = self.lock.locked()
        hits = self.hits
        time_now = time.time()
        # do not use self any more below, to avoid it being updated behind the
        # scene and break the values here
        lag = max(time_now - times.fetch, 0.0)
        idle = max(time_now - times.access, 0.0)
        if locked:
            state = RepoState.UPDATING
        elif lag < times_worker.hot:
            state = RepoState.HOT
        elif lag < times_worker.warm:
            state = RepoState.WARM
        else:
            state = RepoState.COLD
        return RepoStat(state, lag, idle, hits)

    @staticmethod
    def re_data_name():
        return re.compile(r'[0-9a-f]{16}')

class WorkerPaths:
    repos: pathlib.Path
    data: pathlib.Path
    links: pathlib.Path

    def __init__(self, repos: str):
        self.repos = pathlib.Path(repos)
        self.data = self.repos / 'data'
        self.links = self.repos / 'links'

class Worker:
    paths: WorkerPaths
    times: WorkerTimes
    repos: dict[str, Repo]
    lock: asyncio.Lock
    redirect: str

    def __init__(self, repos: str, times: WorkerTimes, redirect: str):
        self.paths = WorkerPaths(repos)
        self.times = times
        self.repos = {}
        self.lock = asyncio.Lock()
        self.redirect = redirect

    async def reset(self):
        print(f"[gacher] resetting '{self.paths.repos}'")
        async with self.lock:
            shutil.rmtree(self.paths.repos)
            self.repos={}
            self.paths.repos.mkdir(parents=True)
            self.paths.data.mkdir()
            self.paths.links.mkdir()

    async def scan(self):
        match_name = Repo.re_data_name()
        print(f"[gacher] scanning '{self.paths.repos}'")
        async with self.lock:
            self.repos={}
            for entry in self.paths.data.glob("*"):
                if not match_name.match(entry.name):
                    continue
                if not entry.is_dir():
                    continue
                if time.time() - (entry / "config").stat().st_mtime > self.times.drop:
                    continue
                (status, child_out) = await run_async_check_with_stdout(
                    'git', 'config', 'remote.origin.url',
                    cwd=entry
                )
                if status:
                    shutil.rmtree(entry)
                    continue
                upstream = child_out.strip().decode('utf-8')
                key = hash_str_to_bytes(upstream)
                if key in self.repos:
                    raise Exception(f"duplicated upstream {upstream}")
                print(f"[gacher] discovered repo '{entry}' for '{upstream}'")
                self.repos[key] = await Repo.new(upstream, self.paths.repos)

    async def cache_repo(self, upstream: str) -> WorkStatus:
        key = hash_str_to_bytes(upstream)
        must_update = False
        async with self.lock:
            if key not in self.repos:
                self.repos[key] = await Repo.new(upstream, self.paths.repos)
                must_update = True
            repo = self.repos[key]
        await repo.hit()
        update_failed = await repo.update(self.times.hot)
        if must_update and update_failed:
            async with self.lock:
                print(f"[gacher] initial update of '{upstream}' failed, removing")
                del self.repos[key]
                shutil.rmtree(repo.paths.data)
                return WorkStatus.BAD
        return WorkStatus.OK

    async def routine_worker(self):
        match_name = Repo.re_data_name()
        step = 0
        while True:
            match step:
                # drop
                case 0:
                    async with self.lock:
                        items = tuple(self.repos.items())
                    time_now = time.time()
                    for (key, repo) in items:
                        if repo.lock.locked():
                            continue
                        if time_now - repo.times.access > self.times.drop:
                            print(f"[gacher] dropping '{repo.upstream}' from run-time cache, the on-disk cache is kept in place")
                            async with self.lock:
                                del self.repos[key]
                    del items
                    step += 1
                # remove
                case 1:
                    async with self.lock:
                        keys = tuple(self.repos.keys())
                    for entry in self.paths.data.glob("*"):
                        if not match_name.match(entry.name):
                            continue
                        if entry.name in keys:
                            continue
                        if not entry.is_dir():
                            continue
                        if time.time() - (entry / "config").stat().st_mtime <= self.times.remove:
                            continue
                        print(f"[gacher] removing '{entry}' from disk")
                        shutil.rmtree(entry)
                    del keys
                    step += 1
                # links
                case 2:
                    removed = True
                    while removed:
                        removed = False
                        for entry in self.paths.links.glob("**"):
                            if entry.is_symlink():
                                if entry.exists():
                                    continue
                                try:
                                    entry.unlink()
                                    print(f"[gacher] removed dead symlink '{entry}'")
                                    removed = True
                                except:
                                    pass
                            elif entry.is_dir():
                                try:
                                    entry.rmdir()
                                    print(f"[gacher] removed empty dir '{entry}'")
                                    removed = True
                                except:
                                    pass
                    step += 1
                # update
                case _:
                    for repo in tuple(self.repos.values()):
                        if repo.lock.locked():
                            continue
                        await repo.update(self.times.warm)
                    step = 0
            await asyncio.sleep(self.times.interval)

    async def stat(self) -> dict[str, RepoStat]:
        stat = {}
        async with self.lock:
            repos = tuple(self.repos.values())
        for repo in repos:
            stat[repo.upstream] = dataclasses.asdict(repo.stat(self.times))
        return stat

    def data_of_upstream(self, upstream: str) -> pathlib.Path:
        return self.paths.repos / RepoPaths.calculate_relative_data(upstream)

@dataclasses.dataclass
class CacheInfo:
    upstream: str
    relative: str
    scheme: str
    host: str
    path: str
    service: str

    @staticmethod
    def scheme_from_host(host: str) -> str:
        hostname = urlparse(f"http://{host}").hostname
        try:
            ipaddress.ip_address(hostname)
            return 'http://'
        except:
            pass
        splitted = hostname.rsplit('.', 1)
        if len(splitted) == 1 or splitted[1] == 'lan' or splitted[1] == 'local':
            return 'http://'
        return "https://"

    @classmethod
    def new(cls, request) -> (typing.Self, web.Response):
        scheme = request.match_info['scheme']
        host = request.match_info['host']
        path = request.match_info['path']
        service = request.match_info['service']
        if scheme:
            if not host:
                host = ''
            if not path:
                path = ''
            if not service:
                service = ''
        else:
            if host:
                host = host.lower()
            else:
                return (None, web.Response(status=403, text="empty host in implicit cache request"))
            scheme = cls.scheme_from_host(host)
            if not path:
                return (None, web.Response(status=403, text="no cachable path in implicit cache request"))
            if not path.endswith('.git'):
                path += '.git'
            if not service:
                return (None, web.Response(status=403, text="no valid git service"))
        match request.method:
            case 'POST':
                match service:
                    case 'git-upload-pack' | 'git-upload-archive' | 'git-receive-pack':
                        pass
                    case _:
                        return (None, web.Response(status=403, text="invalid POST service"))
                pass
            case 'GET':
                pass
            case _:
                return (None, web.Response(status=403, text="invalid request method"))
        return (cls(f"{scheme}{host}/{path}", f"{host}/{path}/{service}{'?' if request.query_string else ''}{request.query_string}", scheme, host, path, service), None)

    def env(self, request) -> dict:
        return {
            "CONTENT_TYPE": request.headers.get('Content-Type', ''),
            "REQUEST_METHOD": request.method,
            "PATH_INFO": f"/{RepoPaths.calculate_relative_data(self.upstream)}/{self.service}",
            "QUERY_STRING": request.query_string,
            "GIT_HTTP_EXPORT_ALL": "",
            "GIT_PROJECT_ROOT": "."
        }

def response_bad_method(path: str, method: str, required: str):
    text = f"method {method} to {path} not allowed, allowing {required}"
    return web.Response(status=403, text=text)

def report_access(request, route: str):
    print(f"[gacher] route {route}: '{request.headers.get('X-Forwarded-Host', request.remote)}' -> {request.method} -> '{request.rel_url}'")


async def proc_to_response(proc, response):
    while True:
        buffer = await proc.stdout.read(0x100000)
        if not buffer:
            break
        await response.write(buffer)

    await proc.wait()
    await response.write_eof()

async def request_to_proc(request, proc):
    proc.stdin.write(await request.read())
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
                    prog='gacher',
                    description='a read-only git caching server',
                    epilog='to access gacher you need to access it via the /cache/ path, e.g. http://gacher.lan:8080/cache/github.com/7Ji/gacher.git; access /help for HTTP help message',
                    formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument('--host', default='0.0.0.0', type=str, help="host to bind to")
    parser.add_argument('--port', default=8080, type=int, help="port to bind to")
    parser.add_argument('--repos', default='repos', type=str, help="path to folder to store repos in, subfolder data would contain real repo, and subfolder links would contain human-friendly links")
    parser.add_argument('--reset', action='store_true', help="on startup, remove everything in {repos}, instead of trying to pick existing repos up")
    parser.add_argument('--time-hot', default=10, type=int, help="time in seconds after which a repo shall be updated when it is being fetched by a client")
    parser.add_argument('--time-warm', default=3600, type=int, help="time in seconds after which a repo shall be updated when it has not been fetched by any client")
    parser.add_argument('--time-drop', default=86400, type=int, help="time in seconds after which a repo shall be dropped/unmanaged if it hasn't been reached by any client")
    parser.add_argument('--time-remove', default=604800, type=int, help="time in seconds after which a unmanaged repo shall be removed/deleted")
    parser.add_argument('--interval', default=1, type=int, help="time interval in seconds to perform routine check and act accordingly to {time_warm}, {time_drop} and {time_remove}")
    parser.add_argument('--redirect', default='', type=str, help="instead of serving the cached repos directly by ourselves, return 301 redirect to such address, useful if you combine gacher with a web frontend, e.g. cgit, it is recommended to use {repos}/links as its root in that case")

    args = parser.parse_args()
    if args.time_remove <= args.time_drop:
        raise ValueError("time_remove must be longer than time_drop")
    if args.time_warm <= args.time_hot:
        raise ValueError("time_warm must be longer than time_hot")

    worker = Worker(args.repos, WorkerTimes(args.time_hot, args.time_warm, args.time_drop, args.time_remove, args.interval), args.redirect)

    async def route_cache(request):
        report_access(request, 'cache')
        cache_info, response = CacheInfo.new(request)
        if response is not None:
            return response
        if await worker.cache_repo(cache_info.upstream):
            return web.Response(status=500, text="failed to cache upstream")
        if worker.redirect:
            redirect = f"{worker.redirect}{cache_info.relative}"
            print(f"[gacher] redirecting to '{redirect}'")
            return web.HTTPMovedPermanently(redirect)

        match request.method:
            case 'GET':
                match cache_info.service:
                    case 'HEAD':
                        async with aiofiles.open(worker.data_of_upstream(cache_info.upstream) / 'HEAD', 'rb') as f:
                            body = await f.read()
                        return web.Response(body = body, content_type='text/plain')
                    case 'info/refs':
                        if request.query.get('service') == 'git-upload-pack':
                            proc = await prepare_to_run_async_with_pipe_out(
                                'git', 'upload-pack', '--strict', '--stateless-rpc', '--http-backend-info-refs',
                                worker.data_of_upstream(cache_info.upstream),
                                env=cache_info.env(request),
                                cwd=worker.paths.repos
                            )
                            response = web.StreamResponse()
                            response.headers.add('Content-Type', 'application/x-git-upload-pack-advertisement')
                            response.headers.add("Cache-Control", "no-cache")
                            await response.prepare(request)
                            await response.write(b"001e# service=git-upload-pack\n0000")
                            await proc_to_response(proc, response)
                            return response
                proc = await prepare_to_run_async_with_pipe_out(
                    'git', 'http-backend',
                    env=cache_info.env(request),
                    cwd=worker.paths.repos
                )
            case 'POST':
                match cache_info.service:
                    case 'git-upload-pack':
                        proc = await prepare_to_run_async_with_pipe_in_out(
                            'git', 'upload-pack', '--strict', '--stateless-rpc',
                            worker.data_of_upstream(cache_info.upstream),
                            env=cache_info.env(request),
                            cwd=worker.paths.repos
                        )
                        await request_to_proc(request, proc)
                        response = web.StreamResponse()
                        await response.prepare(request)
                        await proc_to_response(proc, response)
                        return response
                proc = await prepare_to_run_async_with_pipe_in_out(
                    'git', 'http-backend',
                    env=cache_info.env(request),
                    cwd=worker.paths.repos
                )
                await request_to_proc(request, proc)
            case _:
                return web.Response(status=403, text="invalid method to cache route")
        print(f"[gacher] fall back to git-http-backend: '{request.url}'")
        response = web.StreamResponse()
        for line in (await proc.stdout.readuntil(b'\r\n\r\n'))[:-4].split(b'\r\n'):
            if len(line) == 0:
                continue
            splitted = line.split(b': ', 1)
            if len(splitted) != 2:
                continue
            response.headers.add(splitted[0].decode('latin-1'), splitted[1].decode('latin-1'))
        await response.prepare(request)
        await proc_to_response(proc, response)
        return response

    async def route_uncachable(request):
        report_access(request, 'uncachable')
        return web.Response(status=403, text="uncachable access")

    async def route_stat(request):
        report_access(request, 'stat')
        return web.json_response(await worker.stat())

    async def route_help(request):
        report_access(request, 'help')
        return web.Response(text=textwrap.dedent(f"""
            gacher is a read-only git caching server and you shall access it through the following routing paths (the following examples all use http://gacher.lan:{args.port}/ as the server and remember to adapt it accordingly):

            /help{'' if args.redirect else ', /'}:
                - return this help message

            /cache/:
                - the main caching route, upstream URL shall be appended after it, e.g. http://gacher.lan:{args.port}/cache/github.com/7Ji/ampart.git
                - the real upstream URL is figured out by gacher internally, either with http:// prefix for supposedly remotes in LAN, or with https:// prefix for supposedly remotes from Internet
                - always read-only and you shall never push through the corresponding link
                - when fetching through such cache, if the corresponding repo was already fetched and updated shorter than {args.time_hot} seconds, the local cache would be used
                - if a cached repo was not accessed longer than {args.time_warm} seconds, it would be updated to sync with upstream
                - if a cached repo was not accessed longer than {args.time_drop} seconds, it would be dropped from gacher's run-time storage (but kept on-disk)
                - if a on-disk dropped repo was not accessed longer than {args.time_remove} seconds, it would be removed entirely to free up disk space
                - if '{args.redirect}' is set, after repo cached, instead of serving it directly, a 301 redirect would be returned to it on which e.g. nginx + cgit + git-http-backend is running and performs better than aiohttp

            /stat:
                - return JSON-formatted stat of all repos
        """))

    async def on_startup(app):
        if args.reset:
            await worker.reset()
        else:
            await worker.scan()
        routine_worker = web.AppKey("routine_worker", asyncio.Task)
        app[routine_worker] = asyncio.create_task(worker.routine_worker())

    app = web.Application()
    app.on_startup.append(on_startup)

    if args.redirect:
        async def route_upstream(request):
            report_access(request, 'upstream')
            redirect = f"{worker.redirect}{request.rel_url}"
            return web.HTTPMovedPermanently(redirect)
        route_root = route_upstream
    else:
        route_root = route_help

    cache_prefix = r'/cache/{scheme:((https?|git)://)?}{host:[^/]+[^:/]}/{path:.+}/'
    app.add_routes([
        web.get(cache_prefix + r'{service:(HEAD|info/refs|objects/(info/((http-)?alternates|packs)|[0-9a-f]{2}/([0-9a-f]{38}|[0-9a-f]{62})|pack/pack-([0-9a-f]{40}|[0-9a-f]{64})\.(pack|idx)))}', route_cache),
        web.post(cache_prefix + r'{service:(git-upload-pack|git-upload-archive|git-receive-pack)}', route_cache),
        web.route('*', r'/cache/{anything:.*}', route_uncachable),
        web.get("/stat", route_stat),
        web.route('*', '/help', route_help),
        web.route('*', r'/{upstream:.*}', route_root)
    ])
    web.run_app(app, host=args.host, port=args.port)
