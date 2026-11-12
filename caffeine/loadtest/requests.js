import http from "k6/http";
import { group, sleep, check } from "k6";
import { Trend } from "k6/metrics";
import exec from "k6/execution";

const get_doc_trend = new Trend("get_doc");
const load_list_trend = new Trend("load_list");
const count_list_trend = new Trend("count_list");
const create_si_trend = new Trend("save_si");
const submit_si_trend = new Trend("submit_si");
const make_payment_trend = new Trend("make_payment");
const create_payment_trend = new Trend("save_payment");
const submit_payment_trend = new Trend("submit_payment");
const make_delivery_trend = new Trend("make_delivery");
const save_delivery_trend = new Trend("save_delivery");
const submit_delivery_trend = new Trend("submit_delivery");
// Lets BASE_URL be an IP address, e.g. http://127.0.0.1:8000
export const SITE_HEADERS = __ENV.SITE ? { "X-Frappe-Site-Name": __ENV.SITE } : {};
const ITEMS_PER_INVOICE = parseInt(__ENV.ITEMS_PER_INVOICE) || 3;
const THINK_TIME = parseFloat(__ENV.THINK_TIME ?? "1");

export function think(seconds) {
	if (THINK_TIME > 0) sleep(seconds * THINK_TIME);
}

function get_doc(doctype, name) {
	let fetch_doc = request(
		"/api/method/frappe.desk.form.load.getdoc",
		{ doctype, name },
		get_doc_trend
	);
	if (fetch_doc.status == 200) {
		return JSON.parse(fetch_doc.body)?.docs?.[0];
	}
}

export function sales_invoice_list(config) {
	group("Load Sales Invoice List", function () {
		// This is copied from network tab for realistic file and args.
		let list_args = {
			doctype: "Sales Invoice",
			fields: ["*"],
			filters: [],
			order_by: "`tabSales Invoice`.creation desc",
			start: 0,
			page_length: 20,
			view: "List",
			with_comment_count: 1,
		};
		request("/api/method/frappe.desk.reportview.get", list_args, load_list_trend);
		let count_args = {
			doctype: "Sales Invoice",
			filters: [],
			fields: [],
			distinct: false,
			limit: 1001,
		};
		request("/api/method/frappe.desk.reportview.get_count", count_args, count_list_trend);
	});
}

export function sales_invoice_create(config) {
	let invoice;
	group("Create a new Sales Invoice", function () {
		let warehouse = config.warehouses[(__VU - 1) % config.warehouses.length];
		let tomorow = new Date(new Date().getTime() + 2 * 24 * 60 * 60 * 1000);
		let tomorow_str = `${tomorow.getFullYear()}-${
			tomorow.getMonth() + 1
		}-${tomorow.getDate()}`;
		let tmp_name = `"new-sales-invoice-${getRandomInt(0, 10000000)}`;
		let doc = {
			__islocal: 1,
			__unsaved: 1,
			company: config.company,
			customer: config.customers[getRandomInt(0, config.customers.length)],
			due_date: tomorow_str,
			name: tmp_name,
			status: "Draft",
			debit_to: "Debtors - TC",
			docstatus: 0,
			doctype: "Sales Invoice",
			branch: warehouse.replace("WH-", ""),
			items: [],
		};

		for (let i = 0; i < ITEMS_PER_INVOICE; i++) {
			doc.items.push({
				__islocal: 1,
				__unsaved: 1,
				docstatus: 0,
				doctype: "Sales Invoice Item",
				idx: i + 1,
				name: `new-sales-invoice-item-${getRandomInt(0, 100000000)}`,
				parent: tmp_name,
				parentfield: "items",
				parenttype: "Sales Invoice",
				item_code: config.items[getRandomInt(0, config.items.length)],
				qty: 1,
				rate: i + 1,
				uom: "Nos",
				warehouse: warehouse,
				expense_account: "Cost of Goods Sold - TC",
				cost_center: "Main - TC",
				income_account: "Sales - TC",
			});
		}

		let create_si = request(
			`/api/method/frappe.desk.form.save.savedocs`,
			{
				doc: JSON.stringify(doc),
				action: "Save",
			},
			create_si_trend
		);
		invoice = JSON.parse(create_si.body)["docs"][0];
		get_doc("Sales Invoice", invoice.name);
	});

	return invoice;
}

export function sales_invoice_submit(config, doc) {
	let invoice;

	group("Submit a Sales Invoice", function () {
		let submit_si = request(
			"/api/method/frappe.desk.form.save.savedocs",
			{
				doc: JSON.stringify(doc),
				action: "Submit",
			},
			submit_si_trend
		);
		invoice = JSON.parse(submit_si.body)["docs"][0];
		invoice = get_doc("Sales Invoice", invoice.name);
		check(invoice, { document_is_submitted: (doc) => doc.docstatus == 1 });
	});

	return invoice;
}

export function sales_invoice_payment(config, invoice) {
	group("Create payment for Sales Invoice", function () {
		let make_payment = request(
			"/api/method/erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
			{
				dt: invoice.doctype,
				dn: invoice.name,
				bank_account: "Cash - TC",
			},
			make_payment_trend
		);
		let payment = JSON.parse(make_payment.body).message;
		payment.__islocal = 1;
		payment.branch = invoice.branch;
		payment.name = `new-payment-entry-${getRandomInt(0, 1000000)}`;

		think(0.5);
		let save_payment = request(
			`/api/method/frappe.desk.form.save.savedocs`,
			{
				doc: JSON.stringify(payment),
				action: "Save",
			},
			create_payment_trend
		);
		payment = JSON.parse(save_payment.body).docs[0];

		think(0.5);
		request(
			"/api/method/frappe.desk.form.save.savedocs",
			{
				doc: JSON.stringify(payment),
				action: "Submit",
			},
			submit_payment_trend
		);
		think(0.5);
		invoice = get_doc(invoice.doctype, invoice.name);
		check(invoice, { order_status_paid: (doc) => doc.status === "Paid" });
	});
	return invoice;
}

export function deliver_items(config, invoice) {
	group("Create delivery for Sales Invoice", function () {
		let make_delivery = request(
			"/api/method/frappe.model.mapper.make_mapped_doc",
			{
				method: "erpnext.accounts.doctype.sales_invoice.mapper.make_delivery_note",
				source_name: invoice.name,
			},
			make_delivery_trend
		);
		let delivery = JSON.parse(make_delivery.body).message;
		delivery.__islocal = 1;
		delivery.branch = invoice.branch;
		delivery.name = `new-delivery-note-${getRandomInt(0, 1000000)}`;

		think(0.5);
		let save_delivery = request(
			"/api/method/frappe.desk.form.save.savedocs",
			{
				doc: JSON.stringify(delivery),
				action: "Save",
			},
			save_delivery_trend
		);
		delivery = JSON.parse(save_delivery.body).docs[0];

		think(0.5);
		request(
			"/api/method/frappe.desk.form.save.savedocs",
			{
				doc: JSON.stringify(delivery),
				action: "Submit",
			},
			submit_delivery_trend
		);

		think(0.5);
		invoice = get_doc(invoice.doctype, invoice.name); // check delivery status
	});
}

// =========== Utils ==============

function log_failure(name, res) {
	let exc_type = "unknown";
	try {
		exc_type = JSON.parse(res.body).exc_type || exc_type;
	} catch (e) {}
	console.warn(`FAIL ${name} ${res.status} ${exc_type}`);
}

function getRandomInt(min, max) {
	return Math.floor(Math.random() * (max - min)) + min;
}

export function request(path, payload, trend, expected_status_code = 200) {
	let res = http.post(`${__ENV.BASE_URL}${path}`, JSON.stringify(payload), {
		headers: { "Content-Type": "application/json", ...SITE_HEADERS },
	});

	if (trend) {
		trend.add(res.timings.waiting);
		let tests = {};
		tests[trend.name] = (r) => r.status === expected_status_code;
		let result = check(res, tests);
		if (!result) log_failure(trend.name, res);
		if (!result && __ENV.CI) {
			console.log(JSON.stringify(res, null, 4));
			exec.test.abort("Request failed in CI");
		}
	}
	return res;
}
