# gacher, a read-only git caching server

gacher is a simple read-only git caching server: fetching through gacher server makes the server fetch from upstream first then serve it from the local cache. The cache is updated both routinely and on demand.

## Usage

Just execute the main script `gacher.py`

```
./gacher.py
```

By default the server binds to `http://0.0.0.0:8080`, to use the cache fetch an upstream through the `/cache` route with corresponding path, e.g.:
```
git clone http://127.0.0.1:8080/github.com/7Ji/gacher.git
```

gacher would figure out the upstream `https://github.com/7Ji/gacher.git`, fetch from it with `git` if it does not exist locally at `./repos/data/[hash of upsteam]` or is not new enough, then serve from the local cache `./repos/data/[hash of upstream]` by calling `git-http-backend` as a CGI and bridge the connection.

The script also supports advanced config from command line:

```
gacher [-h] [--host HOST] [--port PORT] [--repos REPOS] [--reset] [--time-hot TIME_HOT]
              [--time-warm TIME_WARM] [--time-drop TIME_DROP] [--time-remove TIME_REMOVE] [--interval INTERVAL]
              [--redirect REDIRECT]
```

Supported arguments are:
- `-h`, `--help`: show help message and exit
- `--host HOST`: host to bind to (default: `0.0.0.0`)
- `--port PORT`: port to bind to (default: `8080`)
- `--repos REPOS`:  path to folder to store repos in, subfolder data would contain real repo, and subfolder links would contain human-friendly links (default: `repos`)
- `--reset`: on startup, remove everything in {repos}, instead of trying to pick existing repos up (default: `False`)
- `--time-hot TIME_HOT` time in seconds after which a repo shall be updated when it is being fetched by a client (default: `10`)
- `--time-warm TIME_WARM` time in seconds after which a repo shall be updated when it has not been fetched by any client (default: `3600`)
- `--time-drop TIME_DROP` time in seconds after which a repo shall be dropped/unmanaged if it hasn't been reached by any client (default: `86400`)
- `--time-remove TIME_REMOVE` time in seconds after which a unmanaged repo shall be removed/deleted (default: `604800`)
- `--interval INTERVAL` time interval in seconds to perform routine check and act accordingly to `{time_warm}`, `{time_drop}` and `{time_remove}` (default: `1`)
- `--redirect REDIRECT` instead of serving the cached repos directly by ourselves, return 301 redirect to such address, useful if you combine gacher with a web frontend, e.g. cgit, it is recommended to use `{repos}`/links as its root in that case (default: )

## Routes / API

gacher has several routes including the main `/cache/` route, these are:
- `/help`, `/` (any method)
  - return pure-text help message about routes
- `/cache/` (any method)
    - the main caching route, upstream URL shall be appended after it, e.g. http://gacher.lan:8080/cache/github.com/7Ji/ampart.git
    - the real upstream URL is figured out by gacher internally, either with http:// prefix for supposedly remotes in LAN, or with https:// prefix for supposedly remotes from Internet
    - always read-only and you shall never push through the corresponding link
    - when fetching through such cache, if the corresponding repo was already fetched and updated shorter than `{time_hot}` seconds (by default 10 seconds), the local cache would be used
    - if a cached repo was not accessed longer than `{time_warm}` seconds (by default 3600 seconds, i.e. 1 hr), it would be updated to sync with upstream
    - if a cached repo was not accessed longer than `{time_drop}` seconds (by default 86400 seconds, i.e. 1 day), it would be dropped from gacher's run-time storage (but kept on-disk)
    - if a on-disk dropped repo was not accessed longer than `{time_remove}` seconds (by default 604800, i.e. 1 week), it would be removed entirely to free up disk space
    - local cache would be considered daed and removed after not being touched for longer than `{time_drop}` seconds (by default 604800 seconds, i.e. 7 days)
    - if `{redirect}` is set, after repo cached, instead of serving it directly, a 301 redirect would be returned to it on which e.g. nginx + cgit + git-http-backend is running and performs better than aiohttp
- `/stat` (`GET`)
    - return JSON-formatted stat of all repos

## Examples

gacher can be used as a standalone server application, or used in combination with a web proxy like nginx

### Standalone

Just run `./gacher.py`, access to cache go through `http://[host]:8080/cache`, cached repos are served by `git-http-backend` called by gacher itself

### With nginx and cgit

As `gacher` maintains both `repos/data/[hash]` to avoid URL path confliction and `repos/links/[upstream]` to simplify lookup, you can combine it with a web frontend serving `repos/links`.

On e.g. Arch Linux, install `nginx`, `cgit`, `python-pygments`, `fcgiwrap`

Configure a server block in nginx like following:
```
server {
    listen       80;
    listen       [::]:80;
    server_name  gacher.lan;
    root         /usr/share/webapps/cgit;
    try_files    $uri @cgit;
    access_log /var/log/nginx/gacher.access.log;

    location ^~ /cache {
        proxy_pass http://127.0.0.1:19418/cache;
        proxy_buffering off;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port $server_port;
    }
        
    location ~ /.+\.git/(info/refs|git-upload-pack).* {
        include             fastcgi_params;
        fastcgi_param       SCRIPT_FILENAME     /usr/lib/git-core/git-http-backend;
        fastcgi_param       PATH_INFO           $uri;
        fastcgi_param       GIT_HTTP_EXPORT_ALL "";
        fastcgi_param       GIT_PROJECT_ROOT    /srv/gacher/repos/links;
        fastcgi_pass        unix:/run/fcgiwrap.sock;
        fastcgi_read_timeout 3600;
        client_max_body_size 50m;
    }

    location @cgit {
        include             fastcgi_params;
        fastcgi_param       SCRIPT_FILENAME /usr/lib/cgit/cgit.cgi;
        fastcgi_param       PATH_INFO       $uri;
        fastcgi_param       QUERY_STRING    $args;
        fastcgi_param       HTTP_HOST       $server_name;
        fastcgi_pass        unix:/run/fcgiwrap.sock;
    }
}
```

Configure cgit with the following `/etc/cgitrc`:
```
enable-http-clone=0
cache-size=10000
cache-root=/var/cache/cgit/$HTTP_HOST
robots=noindex, nofollow
source-filter=/usr/lib/cgit/filters/syntax-highlighting.py
virtual-root=/
include=/etc/cgitrc.d/$HTTP_HOST
```
And the following `/etc/cgitrc.d/cacher.lan`:
```
scan-path=/srv/gacher/repos/links
```
Make sure the user running fcgiwrap has git global config `safe.directory` set to `*`
```
user=$(systemctl show fcgiwrap.service | sed -n 's/^User=\(.\+\)/\1/p')
sudo -u ${user} git config --global safe.directory '*'
```

Then enable both nginx and cgit:
```
sudo systemctl enable --now nginx cgit fcgiwrap.socket
```

Now just run gacher as daemon with `redirect` set to the main entry
```
./gacher.py --port 19418 --repos /srv/gacher/repos --redirect http://gacher.lan/
```

Web traffic goes as following:
- If one accesses `http://gacher.lan` directly, nginx calls cgit which generates the webpage from `/srv/gacher/repos/links`, so you can browse the local cache on web
- If one run e.g. `git clone http://gacher.lan/cache/github.com/7Ji/gacher.git`, nginx passes the request to gacher, and gacher would figure out the upstream `https://github.com/7Ji/gacher.git`, cache it into `repos/data/[hash of upstream]`, creates `repos/links/github.com/7Ji/gacher.git` pointing to data dir, then redirect to `http://gacher.lan/github.com/7Ji/gacher.git`
- Now the traffic is just `git clone http://gacher.lan/github.com/7Ji/gacher.git`, nginx calls `git-http-backend` which serves from `/srv/gacher/repos/links`, one can also clone from this URL directly to bypass gacher

## License
**gacher**, a read-only git caching server

Copyright (C) 2025-present Guoxin "7Ji" Pu

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.