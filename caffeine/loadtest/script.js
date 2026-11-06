import http from "k6/http";
import { check } from "k6";
import {
	deliver_items,
	sales_invoice_create,
	sales_invoice_list,
	sales_invoice_payment,
	sales_invoice_submit,
	request,
	SITE_HEADERS,
	think,
} from "./requests.js";

const NUM_ITEMS = parseInt(__ENV.NUM_ITEMS) || 1000;
const NUM_WAREHOUSES = parseInt(__ENV.NUM_WAREHOUSES) || 10;
const NUM_CUSTOMERS = (parseInt(__ENV.CUSTOMERS_PER_WAREHOUSE) || 3000) * NUM_WAREHOUSES;
const NUM_USERS = (parseInt(__ENV.USERS_PER_WAREHOUSE) || 10) * NUM_WAREHOUSES;
const COMPANY = "The Company";
export const options = {
	vus: Math.min(parseInt(__ENV.VUS) || NUM_USERS, NUM_USERS),
	setupTimeout: "5m",
};

export function setup() {
	// login every user and remember credentials
	// master data is created by python setup counterpart

	const sids = [];
	Array.from(Array(NUM_USERS).keys())
		.map((i) => `u-${String(i + 1).padStart(4, "0")}@erpc.local`)
		.forEach((username) => {
			let res = request("/api/method/login", { usr: username, pwd: username });
			if (res.status != 200) {
				throw new Error(`User login failed for ${username}`);
			}
			sids.push(res.cookies.sid[0].value);
		});

	const items = Array.from(Array(NUM_ITEMS).keys()).map(
		(i) => `I-${String(i + 1).padStart(6, "0")}`
	);
	const warehouses = Array.from(Array(NUM_WAREHOUSES).keys()).map(
		(i) => `WH-${String(i + 1).padStart(4, "0")}`
	);

	const customers = Array.from(Array(NUM_CUSTOMERS).keys()).map(
		(i) => `C-${String(i + 1).padStart(6, "0")}`
	);

	return {
		sids,
		items,
		warehouses,
		customers,
		company: COMPANY,
	};
}

export default function (data) {
	if (data.sids.length != NUM_USERS) {
		console.error("SIDs not available. VU cannot proceed.");
	}
	const jar = http.cookieJar();
	jar.set(__ENV.BASE_URL, "sid", data.sids[__VU - 1]);

	let pong = http.get(`${__ENV.BASE_URL}/api/method/ping`, { headers: SITE_HEADERS });
	check(pong, {
		ping: (r) => r.status === 200 && r.cookies.sid?.[0]?.value == data.sids[__VU - 1],
	});
	think(0.1);
	sales_invoice_list();
	// NOTE: I am assuming API style use case here.
	// For manual entries the think time should be 10-60 seconds at least.
	think(Math.random() * 2);
	let invoice = sales_invoice_create(data);
	think(Math.random() * 2);
	invoice = sales_invoice_submit(data, invoice);
	think(Math.random() * 2);
	invoice = sales_invoice_payment(data, invoice);
	think(Math.random() * 2);
	invoice = deliver_items(data, invoice);
}
