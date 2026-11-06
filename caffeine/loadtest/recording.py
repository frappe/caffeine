"""Helpers the load test harness runs with `bench execute`."""

import json

import frappe
from frappe import recorder


def start():
	recorder.delete()
	recorder.start(capture_doc_events=False, explain=False)


def export(path: str):
	"""Stop recording and write raw recorder requests to `path`.

	Skips `recorder.stop()`, which queues EXPLAIN for every recorded query."""
	frappe.client_cache.set_value(recorder.RECORDER_INTERCEPT_FLAG, False)
	recorder.RecorderConfig.delete()
	requests = list(frappe.cache.hgetall(recorder.RECORDER_REQUEST_HASH).values())
	with open(path, "w") as f:
		json.dump(requests, f, default=str)
	recorder.delete()


def export_errors(path: str, since: str):
	"""Write Error Log entries created after `since` to `path`."""
	errors = frappe.get_all(
		"Error Log",
		filters={"creation": (">=", since)},
		fields=["method", "error", "creation"],
		order_by="creation",
	)
	with open(path, "w") as f:
		json.dump(errors, f, default=str)
