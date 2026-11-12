"""Turn raw run output (k6, py-spy, slow log, recorder) into a summary dict."""

import csv
import gzip
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

WRITE_TRENDS = ("save_si", "submit_si", "save_payment", "submit_payment", "save_delivery", "submit_delivery")
SUBMIT_TRENDS = ("submit_si", "submit_payment", "submit_delivery")
FRAME = re.compile(r"^(?P<function>.*) \((?P<file>[^()]*?)(?::(?P<line>\d+))?\)$")
APP_PATH = re.compile(r"(?:^|/)(?:apps/[^/]+/)?(?P<path>(?:frappe|erpnext|caffeine)/.*\.py)$")
# Frames that only carry a query to the database; the caller above them is the call site.
DB_DRIVER_FILES = ("pymysql/", "MySQLdb/", "psycopg/", "psycopg2/")
REDIS_FILES = ("redis/", "hiredis/")
PLUMBING = (
	"frappe/database/",
	"frappe/query_builder/",
	"frappe/recorder.py",
	"frappe/model/db_query.py",
	"frappe/model/qb_query.py",
	"frappe/__init__.py",
	"frappe/utils/caching.py",
	"frappe/model/utils/__init__.py",
	"frappe/utils/typing_validations.py",
)
DOC_PLUMBING = {
	"load_children_from_db",
	"get_lazy_doc",
	"__get__",
	"get",
	"load_from_db",
	"_load_child_table_from_db",
	"__init__",
	"get_doc",
	"get_cached_doc",
	"_get_doc",
	"get_doc_str",
	"wrapper",
	"reload",
}


def analyze_k6(run_dir: Path, duration_seconds: float) -> dict:
	summary = json.loads((run_dir / "k6.json").read_text())
	metrics = summary["metrics"]
	trends = {
		name: {
			stat: round(value, 1)
			for stat, value in metric.items()
			if stat in ("med", "p(90)", "p(95)", "p(99)", "max")
		}
		for name, metric in metrics.items()
		if "p(95)" in metric and not name.startswith(("http_", "iteration", "group"))
	}
	checks = {
		name: {"passes": check["passes"], "fails": check["fails"]}
		for group in iter_groups(summary["root_group"])
		for name, check in group.get("checks", {}).items()
	}
	submitted = sum(checks.get(name, {}).get("passes", 0) for name in SUBMIT_TRENDS)
	hours = duration_seconds / 3600
	return {
		"iterations": metrics["iterations"]["count"],
		"iterations_per_hour": round(metrics["iterations"]["count"] / hours),
		"submits_per_hour": round(submitted / hours),
		"requests_per_second": round(metrics["http_reqs"]["rate"], 2),
		"failed_request_rate": round(metrics["http_req_failed"]["value"], 4),
		"trends": trends,
		"checks": checks,
		"failures": get_failures(run_dir / "k6.log"),
		"write_p95_per_minute": get_write_p95_per_minute(run_dir / "k6.csv.gz"),
	}


def iter_groups(group: dict):
	yield group
	for child in group.get("groups", {}).values():
		yield from iter_groups(child)


def get_failures(log: Path) -> dict:
	failures = Counter()
	for line in log.read_text(errors="replace").splitlines():
		if match := re.search(r"FAIL (\S+) (\d+) (\S+)", line):
			failures[" ".join(match.groups())] += 1
		elif "level=error" in line and "GoError" not in line:
			failures["script error: " + re.sub(r"^.*msg=\"?|\"? source=.*$", "", line)[:120]] += 1
	return dict(failures.most_common(20))


def get_write_p95_per_minute(path: Path) -> list[float]:
	"""p95 of all write requests per minute; a rising curve is the soak test signal."""
	buckets = defaultdict(list)
	with gzip.open(path, "rt") as f:
		for row in csv.DictReader(f):
			if row["metric_name"] in WRITE_TRENDS:
				buckets[int(row["timestamp"]) // 60].append(float(row["metric_value"]))
	return [round(percentile(values, 95)) for _, values in sorted(buckets.items()) if len(values) >= 5]


def percentile(values: list[float], pct: float) -> float:
	values = sorted(values)
	return values[min(len(values) - 1, int(len(values) * pct / 100))]


def analyze_pyspy(path: Path, top: int = 200) -> dict:
	"""CPU samples by app and the hottest frappe/erpnext functions, inclusive and self."""
	if not path.exists() or not path.stat().st_size:
		return {}
	inclusive, own, by_app = Counter(), Counter(), Counter()
	callers = {"db driver": Counter(), "redis": Counter()}
	total = 0
	for line in path.read_text(errors="replace").splitlines():
		stack, _, count = line.rpartition(" ")
		frames = [parse_frame(frame) for frame in stack.split(";") if not frame.startswith("process ")]
		frames = [frame for frame in frames if frame]
		if not frames:
			continue
		count = int(count)
		total += count
		own[frames[-1]] += count
		owner = get_owner(frames)
		by_app[owner] += count
		if owner in callers:
			callers[owner][get_sample_call_site(frames)] += count
		if any(frame.startswith("erpnext/") for frame in frames):
			by_app["under erpnext (inclusive)"] += count
		for frame in set(frames):
			if frame.startswith(("frappe/", "erpnext/")):
				inclusive[frame] += count

	def share(counter):
		return [[frame, round(100 * samples / total, 2)] for frame, samples in counter.most_common(top)]

	return {
		"samples": total,
		"by_app": dict(share(by_app)),
		"inclusive": share(inclusive),
		"self": share(own),
		"db_callers": share(callers["db driver"]),
		"redis_callers": share(callers["redis"]),
	}


def get_sample_call_site(frames: list[str]) -> str:
	"""Innermost frappe/erpnext frame that is not database or cache plumbing."""
	for frame in reversed(frames):
		if frame.startswith(("frappe/", "erpnext/")) and not frame.startswith(PYSPY_PLUMBING):
			return frame
	return "other"


def parse_frame(frame: str) -> str | None:
	"""`function (path:line)` -> `app/path.py:function`; line numbers drift between commits."""
	match = FRAME.match(frame)
	if not match:
		return None
	app_path = APP_PATH.search(match["file"])
	file = app_path["path"] if app_path else match["file"]
	return f"{file}:{match['function']}"


def get_owner(frames: list[str]) -> str:
	"""Bucket a sample by where the CPU was actually spent (its leaf frame)."""
	leaf = frames[-1]
	if leaf.startswith(DB_DRIVER_FILES):
		return "db driver"
	if leaf.startswith(REDIS_FILES):
		return "redis"
	if leaf.startswith(("erpnext/", "frappe/")):
		return leaf.split("/", 1)[0]
	return "other"


PYSPY_PLUMBING = (
	*PLUMBING,
	"frappe/utils/redis_wrapper.py",
	"frappe/cache_manager.py",
	"frappe/client_cache.py",
	"frappe/utils/data.py",
)


def analyze_slow_log(run_dir: Path, db_name: str, top: int = 15) -> list:
	path = run_dir / "slow.log"
	if not path.exists():
		return []
	queries = defaultdict(lambda: [0, 0.0, 0.0])
	for entry in re.split(r"\n(?=# Time:|# User@Host:)", path.read_text(errors="replace")):
		if f"Schema: {db_name}" not in entry or not (time := re.search(r"Query_time: ([\d.]+)", entry)):
			continue
		sql = " ".join(
			line for line in entry.splitlines() if not line.startswith(("#", "SET timestamp", "use "))
		)
		key = normalize_sql(sql)
		queries[key][0] += 1
		queries[key][1] += float(time[1])
		queries[key][2] = max(queries[key][2], float(time[1]))
	ranked = sorted(queries.items(), key=lambda item: -item[1][1])[:top]
	return [
		{"query": query[:400], "count": c, "total_s": round(t, 2), "max_s": round(m, 2)}
		for query, (c, t, m) in ranked
	]


def analyze_lock_waits(path: Path, top: int = 15) -> list:
	"""Lock wait samples (about one per second waited) grouped by waiting and blocking statement."""
	if not path.exists():
		return []
	pairs = Counter()
	for line in path.read_text().splitlines():
		wait = json.loads(line)
		key = (
			normalize_sql(wait.get("waiting") or "")[:160],
			normalize_sql(wait.get("blocking") or "<idle in transaction>")[:160],
			f"{wait.get('table')} {wait.get('index')} {wait.get('mode')}",
		)
		pairs[key] += 1
	return [
		{"waiting": w, "blocking": b, "lock": lock, "samples": n}
		for (w, b, lock), n in pairs.most_common(top)
	]


def analyze_postgres_log(run_dir: Path, top: int = 15) -> tuple[list, dict]:
	"""Slow statements, deadlocks and long lock waits from the copy of the Postgres server log (LT_DB_LOG)."""
	path = run_dir / "postgres.log"
	if not path.exists():
		return [], {}
	queries = defaultdict(lambda: [0, 0.0, 0.0])
	events = Counter()
	deadlocks = []
	for entry in re.split(r"\n(?=\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", path.read_text(errors="replace")):
		if match := re.search(r"LOG:  duration: ([\d.]+) ms  (?:statement|execute [^:]*): (.*)", entry, re.S):
			seconds = float(match[1]) / 1000
			key = normalize_sql(match[2])
			queries[key][0] += 1
			queries[key][1] += seconds
			queries[key][2] = max(queries[key][2], seconds)
		elif "deadlock detected" in entry:
			events["deadlocks"] += 1
			deadlocks.append(entry)
		elif "still waiting for" in entry:
			events["lock waits over deadlock_timeout"] += 1
	(run_dir / "deadlock.txt").write_text("\n\n".join(deadlocks[-3:]))
	ranked = sorted(queries.items(), key=lambda item: -item[1][1])[:top]
	slow = [
		{"query": q[:400], "count": c, "total_s": round(t, 2), "max_s": round(m, 2)}
		for q, (c, t, m) in ranked
	]
	return slow, dict(events)


def normalize_sql(sql: str) -> str:
	sql = re.sub(r"/\*.*?\*/", "", sql)
	sql = re.sub(r"'(?:[^'\\]|\\.)*'", "?", sql)
	sql = re.sub(r"\b\d+(?:\.\d+)?\b", "?", sql)
	sql = re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?)", sql)
	return re.sub(r"\s+", " ", sql).strip()


def get_endpoint(request: dict) -> str:
	if request.get("event_type") == "Background Job":
		return "job " + request["path"]
	form = request.get("form_dict") or {}
	method = (request.get("path") or "").removeprefix("/api/method/").rsplit(".", 1)[-1]
	if method == "savedocs":
		doc = json.loads(form["doc"]) if isinstance(form.get("doc"), str) else form.get("doc") or {}
		return f"{form.get('action')} {doc.get('doctype')}"
	if method == "make_mapped_doc":
		return "map " + str(form.get("method", "")).rsplit(".", 1)[-1]
	detail = form.get("doctype") or form.get("dt") or ""
	return f"{method} {detail}".strip()


def get_call_site(stack: list[dict]) -> str:
	"""Innermost two non-plumbing frames, e.g. `erpnext/x.py:get_price:10 ← erpnext/y.py:validate:20`."""
	sites = []
	for frame in reversed(stack):
		file = re.sub(r"^(\w+)/(?=\1/)", "", frame["filename"])  # recorder paths start with the app folder
		if file.startswith(PLUMBING) or (
			file == "frappe/model/document.py" and frame["function"] in DOC_PLUMBING
		):
			continue
		sites.append(f"{file}:{frame['function']}:{frame['lineno']}")
		if len(sites) == 2:
			break
	return " ← ".join(sites) or "unknown"


def analyze_recording(path: Path) -> dict:
	"""Per endpoint: median queries per request and the call sites issuing the most queries."""
	with gzip.open(path, "rt") as f:
		requests = json.load(f)
	grouped = defaultdict(list)
	for request in requests:
		grouped[get_endpoint(request)].append(request)

	endpoints = {}
	for endpoint, items in sorted(grouped.items()):
		sites = Counter()
		for request in items:
			for call in request["calls"]:
				sites[get_call_site(call["stack"])] += 1
		endpoints[endpoint] = {
			"requests": len(items),
			"queries": statistics.median(request["queries"] for request in items),
			"query_ms": round(statistics.median(request["time_queries"] for request in items), 1),
			"duration_ms": round(statistics.median(request["duration"] for request in items), 1),
			"call_sites": {site: round(count / len(items), 1) for site, count in sites.most_common()},
		}
	return endpoints


def compare_recordings(small: dict, large: dict, rows_added: int) -> dict:
	"""Queries each extra item row adds, per endpoint and call site: the N+1 list."""
	scaling = {}
	for endpoint, data in large.items():
		if endpoint not in small:
			continue
		base_sites = small[endpoint]["call_sites"]
		per_row = {
			site: round((count - base_sites.get(site, 0)) / rows_added, 2)
			for site, count in data["call_sites"].items()
			if count - base_sites.get(site, 0) >= rows_added
		}
		scaling[endpoint] = {
			"queries_per_row": round((data["queries"] - small[endpoint]["queries"]) / rows_added, 2),
			"call_sites_per_row": dict(sorted(per_row.items(), key=lambda item: -item[1])),
		}
	return scaling
