# Live transcriptions in Nextcloud Talk

### Environment variables

`example.env` contains all the environment variables needed to run the app. You can copy it to `.env` and change the values as needed.
The description of each variable other than AppAPI variables is in the `appinfo/info.xml` file.

### Dev Docker Setup

1. `docker build -t ghcr.io/nextcloud/live_transcription .`
2. Register a HaRP/Docker socket proxy deploy daemon:
   https://docs.nextcloud.com/server/latest/admin_manual/exapps_management/AppAPIAndExternalApps.html
   https://docs.nextcloud.com/server/latest/admin_manual/exapps_management/DeployConfigurations.html
3. Add a ghcr.io override to it: `occ app_api:daemon:registry:add --registry-from=ghcr.io --registry-to=local <deploy_daemon_name>`. Either use a new deploy daemon (with the same underlying DSP) or remember to remove this override later.
4. Register the app: `occ app_api:app:register live_transcription <deploy_daemon_name> --info-xml /path/to/live_transcription/appinfo/info.xml --wait-finish --env LT_HPB_URL=wss://nextcloud.local/standalone-signaling/spreed --env LT_INTERNAL_SECRET=7890`. Other environment variables can be added with `--env <name>=<value>`. Make sure to change the `LT_HPB_URL` to match your setup.

### Dev Bare-metal Setup

1. `cp example.env .env`, adjust the values in `.env` as needed.
2. `python -m venv .venv`
3. `. .venv/bin/activate`
4. `pip install -r requirements_dev.txt`
5. `python ex_app/lib/vosk_server.py`

_in another terminal_

6. `. .venv/bin/activate`
7. `python ex_app/lib/main.py`

_in yet another terminal_

8. Register a manual deploy daemon: `occ app_api:daemon:register manual_install "Manual Install" "manual-install" "http" host.docker.internal "http://nextcloud.local"`. Change `host.docker.internal` to `localhost` if you're not using NC in docker.
9. Register the app: `occ app_api:app:register live_transcription manual_install --json-info "{\"id\":\"live_transcription\",\"name\":\"Live Transcription\",\"daemon_config_name\":\"manual_install\",\"version\":\"0.1.0\",\"secret\":\"12345\",\"port\":23000}" --wait-finish`

### HPB setup

1. The following `docker compose.yml` to set up the High Performance Backend (HPB) server. It is configured for Julius' docker dev setup.

```yaml
services:
  nc-talk:
    image: ghcr.io/nextcloud-releases/aio-talk:latest
    init: true
    ports:
      - 3478:3478/tcp
      - 3478:3478/udp
      - 8081:8081/tcp
    environment:
      SKIP_CERT_VERIFY: true
      NC_DOMAIN: "nextcloud${DOMAIN_SUFFIX}"
      TALK_PORT: 3478
      TURN_SECRET: "1234"
      SIGNALING_SECRET: "4567"
      INTERNAL_SECRET: "7890"
    restart: unless-stopped
```

2. Add location redirects in your nginx config:

```nginx
location /standalone-signaling/ {
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_pass http://nc-talk:8011/;
}

location /standalone-signaling/spreed {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "Upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_pass http://nc-talk:8011/spreed;
}
```

This should be added to the `location /` block of your Nextcloud server. Make sure to change the `proxy_pass` URL to match your setup.
For Julius' docker dev, this should go into `data/nginx/vhost.d/nextcloud.local_location`.

Small issue: The entire nginx stack fails to work if `spreedbackend` is not reachable/resolvable. Using a variable can fix this but I haven't got it to work yet. For now, make sure to have the HPB server running to avoid this issue, or use a static IP address.

6. In Talk, the HPB URL would be `https://nextcloud.local/standalone-signaling/` and the shared secret would be `1234`. Replace `nextcloud.local` here if you're not using the docker dev setup. `http` can also be used but microphone is not allowed in the browser for insecure connections.

TURN server setup would looks like:

|turn:only|localhost:3478|1234|UDP and TCP|
|-|-|-|-|

`localhost` here is the host at which the local PC running the browser should reach the TURN server.

And with that, the HPB should be working.
If it's not, debugging can be done through
1. the logs of the HPB's docker compose: `docker compose logs -f`
2. error messages in the HPB server settings in Talk
3. the browser console when opening a Talk room in the `Network` tab with `WS` filter
4. checking reachability of ports with `nc -zv -w 5 <host> <port>`
