# Live transcriptions in Nextcloud Talk

### Environment variables

`example.env` contains all the environment variables needed to run the app. You can copy it to `.env` and change the values as needed.

1. AppAPI environment variables
2. `LT_HPB_URL` - The URL of the High-Performance Backend (HPB) server, e.g., `wss://nextcloud.local/standalone-signaling/spreed`.
2. `LT_INTERNAL_SECRET` - The internal secret for the High-Performance Backend (HPB) server, not the same as the shared secret.

### Setup

1. `python -m venv .venv`
2. `. .venv/bin/activate`
3. `pip install -r requirements_dev.txt`
4. `python ex_app/lib/main.py`
5. Register a manual deploy daemon: `occ app_api:daemon:register manual_install "Manual Install" "manual-install" "http" host.docker.internal "http://nextcloud.local"`. Change `host.docker.internal` to `localhost` if you're not using NC in docker.
6. Register the app: `occ app_api:app:register live_transcription manual_install --json-info "{\"id\":\"live_transcription\",\"name\":\"Live Transcription\",\"daemon_config_name\":\"manual_install\",\"version\":\"0.1.0\",\"secret\":\"12345\",\"port\":23000}" --wait-finish`

### HPB setup

`ghcr.io/nextcloud-releases/aio-talk:latest` can be used too but it does not support self-signed certificates: https://github.com/nextcloud-snap/nextcloud-snap/wiki/How-to-configure-talk-HPB-with-Docker#create--run-docker-stack

1. `cd hpb/docker/`
2. Change the backend urls in `server.conf` if you're not using Julius' docker dev. Same needs to be done in `docker compose.yml` in coturn replacing `nextcloud.local` with your domain.
3. If not using docker-dev, adjust `docker compose.yml` to not use the `master_default` network.
4. `docker compose up -d`
5. Add location redirects in your nginx config:

```nginx
location /standalone-signaling/ {
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  #set $signaling spreedbackend:8011;
  proxy_pass http://spreedbackend:8011/;
}

location /standalone-signaling/spreed {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "Upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  #set $signaling spreedbackend:8011;
  proxy_pass http://spreedbackend:8011/spreed;
}
```

This should be added to the `location /` block of your Nextcloud server. Make sure to change the `proxy_pass` URL to match your setup.
For Julius' docker dev, this should go into `data/nginx/vhost.d/nextcloud.local_location`.

Small issue: The entire nginx stack fails to work if `spreedbackend` is not reachable/resolvable. Using a variable can fix this but I haven't got it to work yet. For now, make sure to have the HPB server running to avoid this issue, or use a static IP address.

6. In Talk, the HPB URL would be `https://nextcloud.local/standalone-signaling/` and the shared secret would be `340e874f222fc5f58ced59f306b5b3f8`. Replace `nextcloud.local` here if you're not using the docker dev setup. `http` can also be used but microphone is not allowed in the browser for insecure connections.

TURN server setup would looks like:

|turn:only|localhost:3478|0647b96bbfbef17e0b3302ee297e2fbb4741675073a5cf26f54d3ab0cefb7fa5|UDP and TCP|
|-|-|-|-|

`localhost` here is the host at which the local PC running the browser should reach the TURN server.

And with that, the HPB should be working.
If it's not, debugging can be done through
1. the logs of the HPB's docker compose: `docker compose logs -f`
2. error messages in the HPB server settings in Talk
3. the browser console when opening a Talk room in the `Network` tab with `WS` filter
4. checking reachability of ports with `nc -zv -w 5 <host> <port>`
