"""Bench processes and database counters for load test runs."""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path


class LoadBench:
	"""A plain frappe-bench directory: services run from its env, settings come from its site configs."""

	def __init__(self, path: Path, site: str, port: int, workers: int | None = None, threads: int = 4):
		self.path = path.resolve()
		self.site = site
		self.port = port
		self.common_config = json.loads((self.path / "sites" / "common_site_config.json").read_text())
		self.site_config = json.loads((self.site_path / "site_config.json").read_text())
		self.workers = workers or self.common_config.get("gunicorn_workers") or os.cpu_count()
		self.threads = threads
		self.python = str(self.path / "env" / "bin" / "python")
		self.processes: list[subprocess.Popen] = []

	@property
	def site_path(self) -> Path:
		return self.path / "sites" / self.site

	@property
	def db_type(self) -> str:
		return self.site_config.get("db_type", "mariadb")

	@property
	def database(self) -> "Database":
		"""Admin access when LT_DB_ROOT_PASSWORD is set, else only the site's own user."""
		config = {**self.common_config, **self.site_config}
		postgres = self.db_type == "postgres"
		root_password = os.environ.get("LT_DB_ROOT_PASSWORD")
		root_user = os.environ.get("LT_DB_ROOT_USER", "postgres" if postgres else "root")
		connection = {
			"host": config.get("db_host", "127.0.0.1"),
			"socket": config.get("db_socket"),
			"port": int(config.get("db_port") or (5432 if postgres else 3306)),
			"user": root_user if root_password else config["db_user"],
			"password": root_password or config["db_password"],
			"privileged": bool(root_password),
			"log_path": os.environ.get("LT_DB_LOG"),
		}
		return (Postgres if postgres else MariaDB)(connection, config["db_name"])

	def get_app_commits(self) -> dict:
		apps = self.path / "apps"
		return {
			app.name: subprocess.check_output(
				["git", "-C", str(app), "rev-parse", "--short", "HEAD"], text=True
			).strip()
			for app in sorted(apps.iterdir())
			if (app / ".git").exists()
		}

	def frappe(self, *args: str) -> str:
		command = [self.python, "-m", "frappe.utils.bench_helper", "frappe", "--site", self.site, *args]
		return subprocess.check_output(command, cwd=self.path / "sites", text=True)

	def execute(self, method: str, *args: str):
		return self.frappe("execute", method, "--args", json.dumps(list(args)))

	def start(self, run_dir: Path, profile: bool, idle: bool = False):
		"""Start redis if it is down, then one job worker, the scheduler and gunicorn, under py-spy when `profile`."""
		self.start_redis()
		gunicorn = [
			str(self.path / "env" / "bin" / "gunicorn"),
			"--bind", f"127.0.0.1:{self.port}",
			"--workers", str(self.workers),
			"--threads", str(self.threads),
			"--worker-class", "gthread",
			"--timeout", "120",
			"--preload",
			"--chdir", str(self.path / "sites"),
			"frappe.app:application",
		]  # fmt: skip
		worker = [
			self.python,
			"-m",
			"frappe.utils.bench_helper",
			"frappe",
			"worker",
			"--queue",
			"default,short,long",
		]
		scheduler = [self.python, "-m", "frappe.utils.bench_helper", "frappe", "schedule"]
		self.spawn("web", gunicorn, run_dir, profile, idle)
		self.spawn("worker", worker, run_dir, profile)
		self.spawn("scheduler", scheduler, run_dir, False)
		self.wait_until_up()

	def spawn(self, label: str, command: list[str], run_dir: Path, profile: bool, idle: bool = False):
		if profile:
			output = str(run_dir / f"pyspy-{label}.txt")
			options = ["--idle", "--rate", "200"] if idle else ["--rate", "50"]
			command = [
				self.py_spy,
				"record",
				"--subprocesses",
				"--format",
				"raw",
				*options,
				"-o",
				output,
				"--",
				*command,
			]
		log = open(run_dir / f"{label}.log", "w")
		self.processes.append(
			subprocess.Popen(
				command, cwd=self.path / "sites", stdout=log, stderr=subprocess.STDOUT, start_new_session=True
			)
		)

	@property
	def py_spy(self) -> str:
		path = self.path / "env" / "bin" / "py-spy"
		if not path.exists():
			raise RuntimeError(
				f"py-spy is not installed: run {self.path}/env/bin/pip install py-spy, or pass --no-pyspy"
			)
		return str(path)

	def redis(self, name: str, *args: str) -> str:
		command = ["redis-cli", "-u", self.common_config[f"redis_{name}"], *args]
		return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)

	def start_redis(self):
		for name in ("cache", "queue"):
			try:
				self.redis(name, "ping")
			except subprocess.CalledProcessError:
				conf = self.path / "config" / f"redis_{name}.conf"
				subprocess.run(["redis-server", str(conf), "--daemonize", "yes"], check=True)

	def wait_until_up(self):
		import urllib.request

		url = f"http://127.0.0.1:{self.port}/api/method/ping"
		ping = urllib.request.Request(url, headers={"X-Frappe-Site-Name": self.site})
		for _ in range(120):
			try:
				urllib.request.urlopen(ping, timeout=5)
				return
			except OSError:
				time.sleep(1)
		raise RuntimeError("gunicorn did not come up in 2 minutes")

	def stop(self):
		"""Stop py-spy first so it writes its profile, then each service's process group."""
		for process in self.processes:
			process.send_signal(signal.SIGINT if process.args[0].endswith("py-spy") else signal.SIGTERM)
		for process in self.processes:
			try:
				process.wait(timeout=60)
			except subprocess.TimeoutExpired:
				pass
			for sig in (signal.SIGTERM, signal.SIGKILL):
				try:
					os.killpg(process.pid, sig)
					time.sleep(3)
				except ProcessLookupError:
					break
		self.processes = []

	def get_redis_stats(self) -> dict:
		memory = dict(
			line.split(":", 1) for line in self.redis("cache", "info", "memory").split() if ":" in line
		)
		queues = {}
		for key in self.redis("queue", "--scan", "--pattern", "rq:queue:*").split():
			queues[key.removeprefix("rq:queue:")] = int(self.redis("queue", "llen", key))
		failed_keys = self.redis("queue", "--scan", "--pattern", "rq:failed:*").split()
		failed = sum(int(self.redis("queue", "zcard", key)) for key in failed_keys)
		commands = {
			name.removeprefix("cmdstat_"): int(re.search(r"calls=(\d+)", stats)[1])
			for name, stats in (
				line.split(":", 1)
				for line in self.redis("cache", "info", "commandstats").split()
				if ":" in line
			)
		}
		return {
			"cache_used_memory": int(memory["used_memory"]),
			"queued_jobs": queues,
			"failed_jobs": failed,
			"cache_commands": commands,
		}


class Database:
	log_name = "server"

	def __init__(self, config: dict, db_name: str):
		self.config = config
		self.db_name = db_name
		self.log_offset = 0

	@property
	def is_privileged(self) -> bool:
		"""False with only the site's own user, as on a hosted bench: no slow log or lock views."""
		return self.config["privileged"]

	def get_stats(self) -> dict:
		raise NotImplementedError

	def start_slow_log(self, run_dir: Path, threshold_ms: int):
		raise NotImplementedError

	def stop_slow_log(self):
		raise NotImplementedError

	def get_lock_waits(self) -> list[dict]:
		"""Statements waiting on a row lock right now, with the blocking transaction's statement."""
		raise NotImplementedError

	def get_deadlock_report(self) -> str:
		return ""

	def mark_log(self):
		"""Remember where the server log (LT_DB_LOG) ends, so `save_log` copies only this run."""
		if path := self.config["log_path"]:
			self.log_offset = Path(path).stat().st_size

	def save_log(self, run_dir: Path):
		"""Copy the server log written during the run into `run_dir`."""
		if not (path := self.config["log_path"]):
			return
		with open(path, "rb") as log:
			log.seek(self.log_offset)
			with open(run_dir / f"{self.log_name}.log", "wb") as copy:
				shutil.copyfileobj(log, copy)


class MariaDB(Database):
	COUNTERS = (
		"Innodb_deadlocks",
		"Innodb_row_lock_waits",
		"Innodb_row_lock_time",
		"Innodb_row_lock_current_waits",
		"Questions",
		"Slow_queries",
		"Innodb_buffer_pool_reads",
		"Innodb_buffer_pool_read_requests",
		"Innodb_os_log_written",
		"Innodb_log_writes",
		"Innodb_os_log_fsyncs",
		"Com_commit",
	)

	@property
	def client(self) -> list[str]:
		if socket := self.config["socket"]:
			return ["mariadb", "-S", socket, "-u", self.config["user"]]
		return [
			"mariadb",
			"-h",
			self.config["host"],
			"-P",
			str(self.config["port"]),
			"-u",
			self.config["user"],
		]

	@property
	def env(self) -> dict:
		return {**os.environ, "MYSQL_PWD": self.config["password"]}

	def query(self, sql: str) -> str:
		command = [*self.client, "-N", "-B", "-e", sql]
		return subprocess.check_output(command, text=True, env=self.env, stderr=subprocess.DEVNULL)

	def import_dump(self, path: Path):
		"""Load a gzipped dump into the site database as the configured user."""
		gunzip = subprocess.Popen(["gunzip", "-c", str(path)], stdout=subprocess.PIPE)
		subprocess.run([*self.client, self.db_name], stdin=gunzip.stdout, env=self.env, check=True)
		gunzip.wait()

	def get_stats(self) -> dict:
		names = ", ".join(f"'{name}'" for name in self.COUNTERS)
		rows = self.query(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({names})").splitlines()
		return {name.lower(): int(value) for name, value in (row.split("\t") for row in rows)}

	def start_slow_log(self, run_dir: Path, threshold_ms: int):
		"""The server writes the slow log into `run_dir`, so this needs the database on the same host."""
		if not self.is_privileged:
			return
		self.slow_log = run_dir / "slow.log"
		self.query(
			f"SET GLOBAL slow_query_log_file = '{self.slow_log}'; SET GLOBAL long_query_time = {threshold_ms / 1000};"
			" SET GLOBAL log_slow_verbosity = 'query_plan'; SET GLOBAL slow_query_log = 1"
		)

	def stop_slow_log(self):
		if self.is_privileged:
			self.query("SET GLOBAL slow_query_log = 0; SET GLOBAL long_query_time = 10")

	def get_deadlock_report(self) -> str:
		"""InnoDB's latest deadlock, without the raw record dumps."""
		if not self.is_privileged:
			return ""
		status = self.query("SHOW ENGINE INNODB STATUS").replace("\\n", "\n")
		section = re.search(r"LATEST DETECTED DEADLOCK\n-+\n(.*?)\n-+\nTRANSACTIONS", status, re.S)
		lines = section[1].splitlines() if section else []
		return "\n".join(line for line in lines if not re.match(r"\s*\d+: (len|SQL NULL)", line))

	def get_lock_waits(self) -> list[dict]:
		if not self.is_privileged:
			return []
		rows = self.query(
			"SELECT r.trx_query, b.trx_query, l.lock_table, l.lock_index, l.lock_mode, r.trx_wait_started"
			" FROM information_schema.innodb_lock_waits w"
			" JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id"
			" JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id"
			" JOIN information_schema.innodb_locks l ON l.lock_id = w.requested_lock_id"
			f" WHERE l.lock_table LIKE '`{self.db_name}`.%'"
		)
		keys = ("waiting", "blocking", "table", "index", "mode", "since")
		return [dict(zip(keys, row.split("\t"), strict=False)) for row in rows.splitlines()]


class Postgres(Database):
	log_name = "postgres"

	def query(self, sql: str) -> str:
		host, port, user = self.config["host"], str(self.config["port"]), self.config["user"]
		command = [
			"psql",
			"-h",
			host,
			"-p",
			port,
			"-U",
			user,
			"-d",
			self.db_name,
			"-At",
			"-F",
			"\t",
			"-c",
			sql,
		]
		return subprocess.check_output(
			command, text=True, env={**os.environ, "PGPASSWORD": self.config["password"]}
		)

	def get_stats(self) -> dict:
		row = self.query(
			"SELECT deadlocks, xact_commit, xact_rollback, blks_read, blks_hit, conflicts"
			f" FROM pg_stat_database WHERE datname = '{self.db_name}'"
		).strip()
		names = ("deadlocks", "xact_commit", "xact_rollback", "blks_read", "blks_hit", "conflicts")
		stats = dict(zip(names, map(int, row.split("\t")), strict=True))
		wal = self.query("SELECT wal_records, wal_fpi, wal_bytes FROM pg_stat_wal").split("\t")
		stats.update(zip(("wal_records", "wal_fpi", "wal_bytes"), map(int, wal), strict=True))
		stats["wal_fsyncs"] = int(
			self.query("SELECT coalesce(sum(fsyncs), 0) FROM pg_stat_io WHERE object = 'wal'").strip()
		)
		stats["lock_waits_now"] = int(
			self.query(
				f"SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = '{self.db_name}'"
			)
		)
		return stats

	def start_slow_log(self, run_dir: Path, threshold_ms: int):
		if not self.is_privileged:
			return
		self.query(f"ALTER SYSTEM SET log_min_duration_statement = {threshold_ms}")
		self.query("ALTER SYSTEM SET log_lock_waits = on")
		self.query("SELECT pg_reload_conf()")

	def stop_slow_log(self):
		if not self.is_privileged:
			return
		self.query("ALTER SYSTEM RESET log_min_duration_statement")
		self.query("ALTER SYSTEM RESET log_lock_waits")
		self.query("SELECT pg_reload_conf()")

	def get_lock_waits(self) -> list[dict]:
		rows = self.query(
			"SELECT regexp_replace(a.query, '\\s+', ' ', 'g'), regexp_replace(b.query, '\\s+', ' ', 'g'),"
			" l.relation::regclass, l.locktype, l.mode, a.query_start"
			" FROM pg_stat_activity a"
			" CROSS JOIN LATERAL unnest(pg_blocking_pids(a.pid)) AS blocker(pid)"
			" JOIN pg_stat_activity b ON b.pid = blocker.pid"
			" LEFT JOIN pg_locks l ON l.pid = a.pid AND NOT l.granted"
			f" WHERE a.datname = '{self.db_name}' AND a.wait_event_type = 'Lock'"
		)
		keys = ("waiting", "blocking", "table", "index", "mode", "since")
		return [dict(zip(keys, row.split("\t"), strict=False)) for row in rows.splitlines()]
