# Deploying to AWS EC2 (free tier)

This walks a single `t3.micro` (or `t2.micro`, depending on what your
region's free tier offers) from zero to a running deployment of the full
stack -- Postgres, API, worker, scheduler -- using
`docker-compose.prod.yml`. Everything runs on one instance; you run every
step yourself from the AWS console and an SSH session.

**Cost:** within the AWS Free Tier (750 instance-hours/month for 12 months
on new accounts, 30 GB of EBS). After the free year a t3.micro is roughly
$8-10/month. Nothing here uses any paid AWS service beyond the instance
and its disk.

---

## 1. Launch the instance (AWS console)

1. **EC2 → Launch instance.**
2. Name: `jobsched`.
3. AMI: **Ubuntu Server 24.04 LTS (64-bit x86)**.
4. Instance type: **t3.micro** (2 vCPU, 1 GB RAM).
5. Key pair: create one (e.g. `jobsched-key`), download the `.pem`, and
   `chmod 400 jobsched-key.pem` locally.
6. Network settings → **Edit**:
   - Allow **SSH (22)** from *My IP* only.
   - Allow **HTTP (80)** from Anywhere (0.0.0.0/0).
   - Allow **HTTPS (443)** from Anywhere -- only needed for the HTTPS
     option in step 6, but harmless to open now.
   - Do **not** open 5432 or 8000; Postgres is never published and the
     API is reached through port 80 (or Caddy).
7. Storage: bump the root volume to **20 GiB gp3** (images + database +
   logs fit comfortably; still inside the 30 GB free allowance).
8. **Launch instance.**
9. Optional but recommended: **Elastic IPs → Allocate → Associate** with
   the instance, so the address survives stop/start. (Free while the
   instance is running.)

## 2. First login + swap

1 GB of RAM is enough to *run* this stack but tight while `docker build`
compiles wheels, so add 2 GB of swap first:

```bash
ssh -i jobsched-key.pem ubuntu@<ELASTIC_IP>

sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h   # should now show 2.0Gi swap
```

## 3. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu
exit
```

Log back in (`ssh -i jobsched-key.pem ubuntu@<ELASTIC_IP>`) so the group
change takes effect, then confirm: `docker compose version`.

## 4. Get the code and configure secrets

```bash
git clone https://github.com/krish231204/job-scheduler.git
cd job-scheduler

cp .env.prod.example .env.prod
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
nano .env.prod   # paste both values in
```

`JWT_SECRET` is mandatory: with `ENVIRONMENT=production` (which the prod
compose file sets) the app refuses to boot on the default or a short
secret. `.env.prod` is git-ignored -- it never leaves the instance.

**Decide how you'll reach the dashboard** (full explanation in step 6):

- *Bare IP over HTTP* (quickest): uncomment `COOKIE_SECURE=false` in
  `.env.prod`. Without it the session cookie is HTTPS-only and dashboard
  login won't stick over plain HTTP.
- *Domain + HTTPS via Caddy*: leave `COOKIE_SECURE` alone, set `DOMAIN=`
  and `API_BIND=127.0.0.1:8080` instead.

## 5. Build and start

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

First build takes a few minutes on a t3.micro. Then:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps          # db healthy, api/worker/scheduler running, migrate exited (0)
curl -s localhost/health/ready                        # {"status":"ok"}
```

Open `http://<ELASTIC_IP>/register`, create your account, and watch the
dashboard. Optional demo data:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec api python -m scripts.seed
```

## 6. Optional: a domain and automatic HTTPS

Point an `A` record of a domain you control (a free DuckDNS subdomain
works too) at the Elastic IP, then:

1. In `.env.prod`, set `DOMAIN=your.domain.example` and
   `API_BIND=127.0.0.1:8080` (so Caddy owns ports 80/443 and the API is
   not published directly), and remove `COOKIE_SECURE` if you set it.
2. Restart with the `https` profile:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml --profile https up -d --build
```

Caddy obtains and renews Let's Encrypt certificates automatically and
proxies WebSockets transparently, so the live dashboard works unchanged
over `wss://`.

## 7. Day-2 operations

```bash
# Logs
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f api
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f worker scheduler

# Deploy an update
git pull
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build   # runs migrations, restarts changed services

# Scale workers (RAM permitting -- each is capped at 200 MB)
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --scale worker=2

# Database backup (add to cron for real use)
docker compose --env-file .env.prod -f docker-compose.prod.yml exec db pg_dump -U jobsched jobsched | gzip > backup-$(date +%F).sql.gz

# Stop everything (data persists in the named volume)
docker compose --env-file .env.prod -f docker-compose.prod.yml down
```

The API container's built-in healthcheck hits `/health/live`;
`/health/ready` additionally checks database reachability -- use that one
if you later put the instance behind a load balancer.

## Sizing notes (t3.micro)

The compose file caps memory per service (db 300 MB, api 300 MB, worker
200 MB, scheduler 120 MB, Caddy 100 MB) so one runaway process can't OOM
the whole box; the swapfile from step 2 absorbs build-time and burst
pressure. With the default `WORKER_CONCURRENCY=4` this comfortably
sustains the benchmark workload (see "Performance" in the README) --
raise concurrency or add a worker replica only after watching
`docker stats` under your real load.
