"""Load test harness: run k6 against a bench with py-spy, DB counters, slow log and recorder.

	lt.py reset   --bench PATH --site S --save
	lt.py load    --bench PATH --site S --label L --duration 10m [--vus N] [--reset] [--compare RUN]
	lt.py profile --bench PATH --site S --label L [--iterations 10] [--reset] [--compare RUN]
	lt.py compare BASE_RUN HEAD_RUN
	lt.py analyze RUN [--compare RUN]

Runs are written to PATH/logs/loadtest. See the README for the environment variables.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import analyze  # noqa: E402
import report  # noqa: E402
from bench import LoadBench  # noqa: E402

SCRIPT = Path(__file__).parent.parent / "script.js"
PROFILE_ROWS = (1, 11)  # item rows per invoice for the two recorder passes


def main():
	args = get_parser().parse_args()
	if args.command == "analyze":
		run_dir = Path(args.run)
		kind = json.loads((run_dir / "run.json").read_text())["args"]["command"]
		(summarize_load if kind == "load" else summarize_profile)(run_dir, args.compare)
		return
	if args.command == "compare":
		base, head = report.load_summary(Path(args.base)), report.load_summary(Path(args.head))
		print(report.render_comparison(base, head))
		return
	bench = LoadBench(Path(args.bench), args.site, args.port, args.workers, args.threads)
	{"load": run_load, "profile": run_profile, "reset": reset}[args.command](bench, args)


def get_parser():
	parser = argparse.ArgumentParser()
	commands = parser.add_subparsers(dest="command", required=True)
	for name in ("load", "profile", "reset"):
		command = commands.add_parser(name)
		command.add_argument("--bench", required=True, help="frappe-bench directory")
		command.add_argument("--site", required=True)
		command.add_argument("--port", type=int, default=8100, help="Port for the harness's own gunicorn")
		command.add_argument(
			"--workers", type=int, help="gunicorn workers; default gunicorn_workers or CPU count"
		)
		command.add_argument("--threads", type=int, default=4, help="gunicorn threads per worker")
		if name == "reset":
			command.add_argument(
				"--save", action="store_true", help="Take the backup that later resets restore"
			)
			continue
		command.add_argument("--label", required=True)
		command.add_argument("--compare", help="Run directory to diff against")
		command.add_argument("--reset", action="store_true", help="Restore the saved backup first")
	load = commands.choices["load"]
	load.add_argument("--duration", default="5m")
	load.add_argument("--vus", type=int)
	load.add_argument("--items", type=int, default=3, help="Item rows per invoice")
	load.add_argument("--think", type=float, default=1.0, help="Think time multiplier")
	load.add_argument("--slow-ms", type=int, default=200)
	load.add_argument("--no-pyspy", action="store_true")
	load.add_argument(
		"--pyspy-idle", action="store_true", help="Sample waiting threads too, at 200 Hz (wall time, not CPU)"
	)
	commands.choices["profile"].add_argument("--iterations", type=int, default=10)
	rerun = commands.add_parser("analyze", help="Rebuild a run's summary")
	rerun.add_argument("run")
	rerun.add_argument("--compare")
	compare = commands.add_parser("compare")
	compare.add_argument("base")
	compare.add_argument("head")
	return parser


def run_load(bench: LoadBench, args):
	if args.reset:
		reset(bench, args)
	run_dir = make_run_dir(bench, args)
	database = bench.database
	try:
		bench.start(run_dir, profile=not args.no_pyspy, idle=args.pyspy_idle)
		started_at = bench.execute("frappe.utils.now").strip()
		before_db, before_redis = database.get_stats(), bench.get_redis_stats()
		database.mark_log()
		database.start_slow_log(run_dir, args.slow_ms)
		began = time.monotonic()
		with LockSampler(database, run_dir / "lock_waits.jsonl") as sampler:
			run_k6(
				bench,
				run_dir,
				f"--duration={args.duration}",
				vus=args.vus,
				items=args.items,
				think=args.think,
			)
		elapsed = time.monotonic() - began
		database.stop_slow_log()
		after_db, after_redis = database.get_stats(), bench.get_redis_stats()
		bench.execute(
			"caffeine.loadtest.recording.export_errors", str(run_dir / "error_log.json"), started_at
		)
		(run_dir / "deadlock.txt").write_text(database.get_deadlock_report())
		database.save_log(run_dir)
	finally:
		database.stop_slow_log()
		bench.stop()

	run = make_summary(bench, args, elapsed)
	run["db"] = {key: after_db[key] - before_db.get(key, 0) for key in after_db}
	run["redis"] = {
		**after_redis,
		"cache_memory_growth": after_redis["cache_used_memory"] - before_redis["cache_used_memory"],
		"cache_commands": {
			name: calls - before_redis["cache_commands"].get(name, 0)
			for name, calls in after_redis["cache_commands"].items()
			if calls > before_redis["cache_commands"].get(name, 0)
		},
	}
	run["db_name"] = database.db_name
	run["host"] = sampler.host
	(run_dir / "run.json").write_text(json.dumps(run, indent=1))
	summarize_load(run_dir, args.compare)


def summarize_load(run_dir: Path, compare: str | None = None):
	"""Build summary.json from what a load run left on disk; rerunnable after harness changes."""
	summary = json.loads((run_dir / "run.json").read_text())
	summary["k6"] = analyze.analyze_k6(run_dir, get_duration_seconds(summary["args"]["duration"]))
	summary["error_log"] = get_error_log_counts(run_dir / "error_log.json")
	if summary["db_type"] == "postgres":
		summary["slow_queries"], summary["postgres_log"] = analyze.analyze_postgres_log(run_dir)
	else:
		summary["slow_queries"] = analyze.analyze_slow_log(run_dir, summary["db_name"])
	summary["lock_waits"] = analyze.analyze_lock_waits(run_dir / "lock_waits.jsonl")
	for label in ("web", "worker"):
		summary[f"pyspy_{label}"] = analyze.analyze_pyspy(run_dir / f"pyspy-{label}.txt")
	write_summary(run_dir, summary, compare)


class LockSampler(threading.Thread):
	"""Poll the database's lock waits once a second while the load runs."""

	def __init__(self, database, path: Path):
		super().__init__(daemon=True)
		self.database = database
		self.path = path
		self.done = threading.Event()

	def __enter__(self):
		self.start()
		return self

	def __exit__(self, *exc):
		self.done.set()
		self.join()

	def run(self):
		self.load_averages = []
		steal_before = get_cpu_steal()
		with open(self.path, "w") as f:
			while not self.done.wait(1):
				self.load_averages.append(float(Path("/proc/loadavg").read_text().split()[0]))
				for wait in self.database.get_lock_waits():
					f.write(json.dumps(wait) + "\n")
		steal, total = (after - before for after, before in zip(get_cpu_steal(), steal_before, strict=True))
		self.host = {
			"cpus": os.cpu_count(),
			"load_avg_mean": round(sum(self.load_averages) / max(len(self.load_averages), 1), 2),
			"load_avg_max": max(self.load_averages, default=0),
			"cpu_steal_pct": round(100 * steal / max(total, 1), 2),
		}


def get_cpu_steal() -> tuple[int, int]:
	"""(steal, total) jiffies from /proc/stat; steal is time the hypervisor gave to other guests."""
	values = [int(value) for value in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
	return values[7], sum(values)


def run_profile(bench: LoadBench, args):
	"""Deterministic pass: one VU, no think time, recorder on; repeated at two invoice sizes."""
	if args.reset:
		reset(bench, args)
	run_dir = make_run_dir(bench, args)
	try:
		bench.start(run_dir, profile=False)
		for rows in PROFILE_ROWS:
			bench.execute("caffeine.loadtest.recording.start")
			run_k6(
				bench,
				run_dir,
				f"--iterations={args.iterations}",
				vus=1,
				items=rows,
				think=0,
				suffix=f"-{rows}",
			)
			time.sleep(5)  # let queued jobs finish so they are recorded too
			path = run_dir / f"recording-{rows}.json"
			bench.execute("caffeine.loadtest.recording.export", str(path))
			subprocess.run(["gzip", str(path)], check=True)
	finally:
		bench.stop()

	(run_dir / "run.json").write_text(json.dumps(make_summary(bench, args, 0), indent=1))
	summarize_profile(run_dir, args.compare)


def summarize_profile(run_dir: Path, compare: str | None = None):
	summary = json.loads((run_dir / "run.json").read_text())
	small, large = (analyze.analyze_recording(run_dir / f"recording-{rows}.json.gz") for rows in PROFILE_ROWS)
	summary["recording"] = large
	summary["recording_small"] = small
	summary["scaling"] = analyze.compare_recordings(small, large, PROFILE_ROWS[1] - PROFILE_ROWS[0])
	write_summary(run_dir, summary, compare)


def run_k6(
	bench: LoadBench, run_dir: Path, limit: str, vus: int | None, items: int, think: float, suffix: str = ""
):
	command = [
		get_k6(), "run", "--quiet", limit,
		"--summary-trend-stats=avg,med,p(90),p(95),p(99),max",
		f"--summary-export={run_dir / f'k6{suffix}.json'}",
		f"--out=csv={run_dir / f'k6{suffix}.csv.gz'}",
		*(["--vus", str(vus)] if vus else []),
		"-e", f"BASE_URL=http://127.0.0.1:{bench.port}",
		"-e", f"SITE={bench.site}",
		"-e", f"ITEMS_PER_INVOICE={items}",
		"-e", f"THINK_TIME={think}",
		*get_scale_env(bench),
		str(SCRIPT),
	]  # fmt: skip
	with open(run_dir / f"k6{suffix}.log", "w") as log:
		subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)


def get_k6() -> str:
	k6 = os.environ.get("K6") or shutil.which("k6")
	if not k6:
		raise RuntimeError("k6 not found: install it or set K6 to its path")
	return k6


def get_scale_env(bench: LoadBench) -> list[str]:
	"""Match k6 to the master data the site actually has."""
	scale = json.loads((bench.site_path / "loadtest.json").read_text())
	return [part for key, value in scale.items() for part in ("-e", f"{key}={value}")]


def reset(bench: LoadBench, args):
	backups = bench.site_path / "private" / "backups"
	marker = bench.site_path / "loadtest-baseline.txt"
	if getattr(args, "save", False):
		bench.start_redis()
		bench.frappe("backup")
		latest = max(backups.glob("*-database.sql.gz"), key=lambda path: path.stat().st_mtime)
		marker.write_text(str(latest))
		print(f"Saved {latest}")
		return
	database = bench.database
	if not database.is_privileged:
		if bench.db_type == "postgres":
			raise RuntimeError("Resetting a Postgres site needs LT_DB_ROOT_PASSWORD")
		database.import_dump(Path(marker.read_text().strip()))
		bench.start_redis()
		bench.frappe("clear-cache")
		return
	bench.frappe(
		"--force", "restore", marker.read_text().strip(),
		"--db-root-username", database.config["user"],
		"--db-root-password", database.config["password"],
	)  # fmt: skip


def make_run_dir(bench: LoadBench, args) -> Path:
	run_dir = bench.path / "logs" / "loadtest" / f"{datetime.now():%Y%m%d-%H%M}-{args.label}"
	run_dir.mkdir(parents=True)
	return run_dir


def make_summary(bench: LoadBench, args, elapsed: float) -> dict:
	return {
		"label": args.label,
		"db_type": bench.db_type,
		"commits": bench.get_app_commits(),
		"args": {key: value for key, value in vars(args).items() if key not in ("bench", "site", "compare")},
		"elapsed_s": round(elapsed),
	}


def write_summary(run_dir: Path, summary: dict, compare: str | None):
	(run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
	(run_dir / "summary.md").write_text(report.render_run(summary))
	if compare:
		comparison = report.render_comparison(report.load_summary(Path(compare)), summary)
		(run_dir / "compare.md").write_text(comparison)
	print(run_dir)


def get_error_log_counts(path: Path) -> dict:
	counts = {}
	for error in json.loads(path.read_text()):
		key = error["method"] or (error["error"] or "").strip().splitlines()[-1][:120]
		counts[key] = counts.get(key, 0) + 1
	return dict(sorted(counts.items(), key=lambda item: -item[1]))


def get_duration_seconds(duration: str) -> float:
	units = {"s": 1, "m": 60, "h": 3600}
	return sum(int(value) * units[unit] for value, unit in re.findall(r"(\d+)([smh])", duration))


if __name__ == "__main__":
	main()
