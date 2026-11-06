"""Markdown for one run, and the regression diff between two runs."""

import json
import re
from pathlib import Path

# A change is flagged when it moves this much, relative and absolute.
LATENCY_THRESHOLD = (0.10, 50)  # 10% and 50 ms
CPU_THRESHOLD = {"web": 1.0, "worker": 3.0}  # percentage points of samples; worker runs vary by job
QUERY_THRESHOLD = 1  # queries per request


def load_summary(run_dir: Path) -> dict:
	return json.loads((run_dir / "summary.json").read_text())


def render_run(summary: dict) -> str:
	lines = [
		f"# {summary['label']} ({summary['db_type']})",
		"",
		f"Apps: {summary['commits']}",
		f"Args: {summary['args']}",
		"",
	]
	if k6 := summary.get("k6"):
		lines += render_k6(k6)
	if host := summary.get("host"):
		lines += [f"Host: {host}", ""]
	if events := summary.get("postgres_log"):
		lines += [f"Postgres log: {events}", ""]
	if stats := summary.get("db"):
		lines += [
			"## Database counters (delta)",
			"",
			*[f"- {key}: {value}" for key, value in stats.items()],
			"",
		]
	if redis := summary.get("redis"):
		commands = redis.get("cache_commands", {})
		iterations = max(summary.get("k6", {}).get("iterations", 0), 1)
		per_iteration = {
			name: round(calls / iterations, 1)
			for name, calls in sorted(commands.items(), key=lambda item: -item[1])
		}
		other = {key: value for key, value in redis.items() if key != "cache_commands"}
		lines += [
			"## Background",
			"",
			f"- {other}",
			f"- redis cache commands per iteration: {per_iteration}",
			"",
		]
	if errors := summary.get("error_log"):
		lines += [
			"## Error Log (by method)",
			"",
			*[f"- {count} × {method}" for method, count in errors.items()],
			"",
		]
	if slow := summary.get("slow_queries"):
		lines += ["## Slow queries", "", "| count | total s | max s | query |", "|---|---|---|---|"]
		lines += [f"| {q['count']} | {q['total_s']} | {q['max_s']} | `{q['query'][:200]}` |" for q in slow]
		lines.append("")
	if waits := summary.get("lock_waits"):
		lines += [
			"## Lock waits (samples ≈ seconds waited)",
			"",
			"| samples | lock | waiting | blocking (its current statement) |",
			"|---|---|---|---|",
		]
		lines += [
			f"| {w['samples']} | {w['lock']} | `{w['waiting'][:120]}` | `{w['blocking'][:120]}` |"
			for w in waits
		]
		lines.append("")
	for label in ("web", "worker"):
		if profile := summary.get(f"pyspy_{label}"):
			lines += render_profile(label, profile)
	if recording := summary.get("recording"):
		lines += render_recording(recording, summary.get("scaling", {}))
	return "\n".join(lines)


def render_k6(k6: dict) -> list[str]:
	lines = [
		"## Throughput",
		"",
		f"- iterations: {k6['iterations']} ({k6['iterations_per_hour']}/h), submits/h: {k6['submits_per_hour']}, req/s: {k6['requests_per_second']}",
		f"- failed requests: {k6['failed_request_rate']:.2%}",
		f"- write p95 per minute (ms): {k6['write_p95_per_minute']}",
		"",
		"## Latency (ms, server wait time)",
		"",
		"| trend | med | p90 | p95 | p99 | max |",
		"|---|---|---|---|---|---|",
	]
	for name, trend in k6["trends"].items():
		lines.append(
			f"| {name} | {trend['med']} | {trend['p(90)']} | {trend['p(95)']} | {trend['p(99)']} | {trend['max']} |"
		)
	lines.append("")
	if k6["failures"]:
		lines += [
			"## Failures",
			"",
			*[f"- {count} × {failure}" for failure, count in k6["failures"].items()],
			"",
		]
	return lines


def render_profile(label: str, profile: dict) -> list[str]:
	lines = [
		f"## py-spy {label} ({profile['samples']} samples)",
		"",
		f"By app: {profile['by_app']}",
		"",
		"Inclusive % (frappe/erpnext):",
		"",
	]
	lines += [f"- {pct}% {frame}" for frame, pct in profile["inclusive"][:30]]
	lines += ["", "Self %:", "", *[f"- {pct}% {frame}" for frame, pct in profile["self"][:15]], ""]
	for key, title in (("db_callers", "DB time by caller"), ("redis_callers", "Redis time by caller")):
		lines += [
			f"{title} (% of all samples):",
			"",
			*[f"- {pct}% {frame}" for frame, pct in profile.get(key, [])[:15]],
			"",
		]
	return lines


def render_recording(recording: dict, scaling: dict) -> list[str]:
	lines = [
		"## Queries per request (recorder)",
		"",
		"| endpoint | n | queries | +queries/row | query ms | total ms |",
		"|---|---|---|---|---|---|",
	]
	for endpoint, data in recording.items():
		per_row = scaling.get(endpoint, {}).get("queries_per_row", "")
		lines.append(
			f"| {endpoint} | {data['requests']} | {data['queries']} | {per_row} | {data['query_ms']} | {data['duration_ms']} |"
		)
	lines += ["", "## Top call sites per endpoint (queries per request)", ""]
	for endpoint, data in recording.items():
		if data["queries"] >= 50:
			lines += [
				f"### {endpoint}",
				"",
				*[f"- {count} {site}" for site, count in list(data["call_sites"].items())[:15]],
				"",
			]
	lines += ["## Queries added per item row, by call site (N+1)", ""]
	for endpoint, data in scaling.items():
		if data["call_sites_per_row"]:
			lines += [
				f"### {endpoint}",
				"",
				*[f"- {count} {site}" for site, count in data["call_sites_per_row"].items()],
				"",
			]
	return lines


def render_comparison(base: dict, head: dict) -> str:
	"""Regressions first, then improvements, so the top of the file answers 'what got worse'."""
	worse, better = [], []
	compare_latency(base, head, worse, better)
	compare_scalars(base, head, worse, better)
	compare_profiles(base, head, worse, better)
	compare_queries(base, head, worse, better)
	lines = [
		f"# {base['label']} → {head['label']} ({head['db_type']})",
		"",
		f"Base: {base['commits']}",
		f"Head: {head['commits']}",
		"",
	]
	lines += [
		"## Regressions",
		"",
		*(worse or ["- none"]),
		"",
		"## Improvements",
		"",
		*(better or ["- none"]),
		"",
	]
	return "\n".join(lines)


def compare_latency(base, head, worse, better):
	base_trends, head_trends = base.get("k6", {}).get("trends", {}), head.get("k6", {}).get("trends", {})
	for name, trend in head_trends.items():
		for stat in ("p(95)", "p(99)"):
			old, new = base_trends.get(name, {}).get(stat), trend[stat]
			if old is None:
				continue
			delta = new - old
			if abs(delta) >= LATENCY_THRESHOLD[1] and abs(delta) >= LATENCY_THRESHOLD[0] * old:
				(worse if delta > 0 else better).append(
					f"- {name} {stat}: {old} → {new} ms ({delta / old:+.0%})"
				)


def compare_scalars(base, head, worse, better):
	for key, higher_is_better in (
		("iterations_per_hour", True),
		("submits_per_hour", True),
		("failed_request_rate", False),
	):
		old, new = base.get("k6", {}).get(key), head.get("k6", {}).get(key)
		if not old or new is None or abs(new - old) < 0.05 * old:
			continue
		(better if (new > old) == higher_is_better else worse).append(
			f"- {key}: {old} → {new} ({(new - old) / old:+.0%})"
		)
	for key in ("innodb_deadlocks", "deadlocks"):
		old, new = base.get("db", {}).get(key), head.get("db", {}).get(key)
		if old is not None and new != old:
			(worse if new > old else better).append(f"- {key}: {old} → {new}")


def compare_profiles(base, head, worse, better):
	for label in ("web", "worker"):
		old = dict(base.get(f"pyspy_{label}", {}).get("inclusive", []))
		new = dict(head.get(f"pyspy_{label}", {}).get("inclusive", []))
		for frame in old.keys() | new.keys():
			delta = new.get(frame, 0) - old.get(frame, 0)
			if abs(delta) >= CPU_THRESHOLD[label]:
				text = f"- py-spy {label} {frame}: {old.get(frame, 0)}% → {new.get(frame, 0)}%"
				(worse if delta > 0 else better).append(text)


def compare_queries(base, head, worse, better):
	old, new = base.get("recording", {}), head.get("recording", {})
	for endpoint in old.keys() & new.keys():
		delta = new[endpoint]["queries"] - old[endpoint]["queries"]
		if abs(delta) >= QUERY_THRESHOLD:
			text = f"- {endpoint}: {old[endpoint]['queries']} → {new[endpoint]['queries']} queries/request"
			(worse if delta > 0 else better).append(text)
			sites_old, sites_new = (
				by_function(old[endpoint]["call_sites"]),
				by_function(new[endpoint]["call_sites"]),
			)
			for site in sites_old.keys() | sites_new.keys():
				site_delta = sites_new.get(site, 0) - sites_old.get(site, 0)
				if abs(site_delta) >= QUERY_THRESHOLD:
					(worse if site_delta > 0 else better).append(
						f"  - {site}: {sites_old.get(site, 0)} → {sites_new.get(site, 0)}"
					)


def by_function(sites: dict) -> dict:
	"""Drop line numbers from call sites so they match across commits."""
	merged = {}
	for site, count in sites.items():
		key = re.sub(r":\d+", "", site)
		merged[key] = round(merged.get(key, 0) + count, 1)
	return merged
