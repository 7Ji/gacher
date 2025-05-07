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
from urllib.parse import urlparse
import xxhash

class WorkStatus(int, enum.Enum):
    OK = 0
    BAD = 1

async def run_async_check(program, *args, max_tries=1, **kwds) -> WorkStatus:
    for _ in range(max_tries):
        proc = await asyncio.create_subprocess_exec\
        (
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

async def run_async_check_with_stdout(
    program, *args, max_tries=1, **kwds
) -> (WorkStatus, bytes):
    for _ in range(max_tries):
        proc = await asyncio.create_subprocess_exec\
        (
            program, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
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
        print(f"[gacher] added repo '{self.upstream}' -> \
                                    '{self.paths.relative_data}'"
        )

    async def ensure_exist(self):
        print(f"[gacher] ensuring local repo existence of '{self.upstream}'")
        if not self.paths.data.is_dir():
            print(f"[gacher] local repo for '{self.upstream}' does not exist, \
                    creating '{self.paths.data}'")
            self.paths.data.mkdir(parents=True)
            if await run_async_check('git', 'init', '--bare', self.paths.data) \
                or \
                await run_async_check\
                (
                    'git', 'remote', 'add', '--mirror=fetch',
                    'origin', self.upstream,
                    cwd=self.paths.data
                ):
                raise Exception("failed to init local repo")
        self.paths.link.parent.mkdir(parents=True, exist_ok=True)
        if self.paths.link.exists():
            self.paths.link.unlink()
        self.paths.link.symlink_to\
        (
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

    async def touch(self):
        async with self.lock:
            self.times.access = time.time()
            self.paths.config.touch()
            self.hits += 1

    def need_update(self, time_hot: float) -> bool:
        return time.time() - self.times.fetch > time_hot

    # this is only called in update(), lock was aquired there
    async def update_inner(self):
        print(f"[gacher] updating '{self.upstream}'")
        # git complains if remote origin was added with --mirror without =fetch
        # or =push, so we added it with --mirror=fetch, that results in HEAD not
        # being updated during fetch, to fix it we set remote.origin.mirror=true
        # manually so fetch also updates HEAD
        if await run_async_check\
        (
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
            return
        self.fetch = time.time()
        print(f"[gacher] updated '{self.upstream}'")

    async def update(self, time_hot: float):
        if not self.need_update(time_hot):
            return
        async with self.lock:
            if not self.need_update(time_hot):
                return
            await self.update_inner()

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

    async def get_repo(self, upstream: str) -> Repo:
        key = hash_str_to_bytes(upstream)
        async with self.lock:
            if key not in self.repos:
                self.repos[key] = await Repo.new(upstream, self.paths.repos)
            repo = self.repos[key]
        return repo

    async def update_repo(self, upstream: str):
        repo = await self.get_repo(upstream)
        await repo.touch()
        # if not repo.need_update(self.times.hot):
        #     print(f"[gacher] serving cached '{upstream}'")
        #     return
        await repo.update(self.times.hot)

    async def routine_worker(self):
        match_name = Repo.re_data_name()
        while True:
            # drop
            async with self.lock:
                items = tuple(self.repos.items())
            time_now = time.time()
            for (upstream, repo) in items:
                if repo.lock.locked():
                    continue
                if time_now - repo.times.access > self.times.drop:
                    async with self.lock:
                        del self.repos[upstream]
            # remove
            for entry in self.paths.data.glob("*"):
                if not match_name.match(entry.name):
                    continue
                if not entry.is_dir():
                    continue
                if time.time() - entry.stat().st_ctime <= self.times.remove:
                    continue
                shutil.rmtree(entry)
            # links
            for entry in self.paths.links.glob("**"):
                if not entry.name.endswith(".git"):
                    continue
                if not entry.is_symlink():
                    continue
                if entry.exists():
                    continue
                entry.unlink()
            # update
            for repo in tuple(self.repos.values()):
                if repo.lock.locked():
                    continue
                if repo.need_update(self.times.warm):
                    await repo.update(self.times.hot)
            await asyncio.sleep(self.times.interval)

    async def stat(self) -> dict[str, RepoStat]:
        stat = {}
        time_now = time.time()
        async with self.lock:
            for repo in self.repos.values():
                stat[repo.upstream] = dataclasses.asdict(repo.stat(time_now, self.times))
        return stat

def is_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ipv4invalid)
        return True
    except:
        return False

def response_bad_method(path: str, method: str, required: str):
    text = f"access to {path} with method {method} is not supported, {required} GET is supported"
    return web.Response(status=403, text=text)

async def route_cache(request):
    upstream: str = request.match_info.get('upstream', '')
    if upstream.endswith("/HEAD"):
        if request.method != "GET":
            return response_bad_method("/HEAD", request.method, "GET")
        upstream = upstream[:-5]
        tail="/HEAD"
    elif upstream.endswith("/info/refs"):
        if request.method != "GET":
            return response_bad_method("/info/refs", request.method, "GET")
        upstream = upstream[:-10]
        tail="/info/refs"
    elif upstream.endswith("/git-upload-pack"):
        if request.method != "POST":
            return response_bad_method("/git-upload-pack", request.method, "POST")
        upstream = upstream[:-16]
        tail="/git-upload-pack"
    else:
        return web.Response(status=403, text=f"service {upstream} not supported")
    if not upstream.endswith(".git"):
        upstream += ".git"
    upstream_redirectable = upstream
    upstream_splitted = upstream.split("/", maxsplit=1)
    try:
        path = upstream_splitted[1]
    except:
        path = ''
    host = upstream_splitted[0].lower()
    if is_ip(host) or host.endswith(".lan") or not '.' in host:
        scheme = 'http://'
    else:
        scheme = 'https://'
    upstream = f"{scheme}{host}/{path}"
    await worker.update_repo(upstream)
    if worker.redirect:
        redirect = f"{worker.redirect}{upstream_redirectable}{tail}?{request.query_string}"
        print(f"[gacher] redirecting to '{redirect}'")
        return web.HTTPMovedPermanently(redirect)
    response = web.StreamResponse()
    env = {
        "CONTENT_TYPE": request.headers.get('Content-Type', ''),
        "REQUEST_METHOD": request.method,
        "PATH_INFO": f"/{RepoPaths.calculate_relative_data(upstream)}{tail}",
        "QUERY_STRING": request.query_string,
        "GIT_HTTP_EXPORT_ALL": "",
        "GIT_PROJECT_ROOT": "."
    }
    if request.method == "POST":
        proc = await asyncio.create_subprocess_exec(
            'git', 'http-backend',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
            cwd=worker.paths.repos
        )
        proc.stdin.write(await request.read())
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()
    else:
        proc = await asyncio.create_subprocess_exec(
            'git', 'http-backend',
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            env=env,
            cwd=worker.paths.repos
        )
    end_header = False
    buffer_all = bytearray()
    while True:
        buffer = await proc.stdout.read(65536)
        if not buffer:
            break
        if end_header:
            await response.write(buffer)
        else:
            buffer_all += buffer
            splitted = buffer_all.split(b'\r\n\r\n', 1)
            if len(splitted) > 1:
                for line in splitted[0].split(b'\r\n'):
                    if len(line) == 0:
                        continue
                    key, value = line.split(b': ', maxsplit=1)
                    response.headers.add(key.decode('utf-8'), value.decode('utf-8'))
                await response.prepare(request)
                await response.write(splitted[1])
                end_header = True

    await proc.wait()

    return response

async def route_stat(request):
    return web.json_response(await repos.stat())

async def route_help(request):
    return web.Response(text=textwrap.dedent("""
        gacher is a read-only git caching server and you shall access it through the following routing paths (the following examples all use http://gacher.lan:8080/ as the server and remember to adapt it accordingly):


        /help, /:
            - return this help message

        /cache/:
            - the main caching route, upstream URL shall be appended after it, e.g. http://gacher.lan:8080/cache/github.com/7Ji/ampart.git
            - the real upstream URL is figured out by gacher internally, either with http:// prefix for supposedly remotes in LAN, or with https:// prefix for supposedly remotes from Internet
            - always read-only and you shall never push through the corresponding link
            - when fetching through such cache, if the corresponding repo was already fetched and updated shorter than {time_hot} seconds (by default 10 seconds), the local cache would be used
            - if a cached repo was not accessed longer than {time_warm} seconds (by default 3600 seconds, i.e. 1 hr), it would be updated to sync with upstream
            - if a cached repo was not accessed longer than {time_drop} seconds (by default 86400 seconds, i.e. 1 day), it would be dropped from gacher's run-time storage (but kept on-disk)
            - if a on-disk dropped repo was not accessed longer than {time_remove} seconds (by default 604800, i.e. 1 week), it would be removed entirely to free up disk space
            - local cache would be considered daed and removed after not being touched for longer than {time_drop} seconds (by default 604800 seconds, i.e. 7 days)
            - if {redirect} is set, after repo cached, instead of serving it directly, a 301 redirect would be returned to it on which e.g. nginx + cgit + git-http-backend is running and performs better than aiohttp

        /stat:
            - return JSON-formatted stat of all repos
    """))

async def route_upstream(request):
    redirect = f"{repos.redirect}{request.match_info.get('upstream', '')}?{request.query_string}"
    return web.HTTPMovedPermanently(redirect)

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
        route_root = route_upstream
    else:
        route_root = route_help

    app.add_routes([
        web.route('*', r'/cache/{upstream:.+}', route_cache),
        web.get("/stat", route_stat),
        web.route('*', '/help', route_help),
        web.route('*', r'/{upstream:.*}', route_root)
    ])
    web.run_app(app, host=args.host, port=args.port)
