import itertools
from copy import deepcopy
from dataclasses import dataclass

import frappe
import tqdm
from erpnext.setup.utils import get_root_of
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import StockReconciliation
from frappe.core.doctype.doctype.doctype import make_property_setter
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.model.document import bulk_insert
from frappe.permissions import AUTOMATIC_ROLES
from frappe.utils import now_datetime

COMPANY_NAME = "The Company"
ITEM_NAME = "I-{}"
WAREHOUSE_NAME = "WH-{}"
CUSTOMER_NAME = "C-{}"
USER_NAME = "u-{}@erpc.local"


@dataclass
class Setup:
	n_items: int = 1000  # TPC-C says 100k, but smaller number will trigger contention faster
	n_warehouses: int = 10  # Note: This is only real "scaling factor" that should be used.
	users_per_warehouse: int = 10
	# TPC-C says 30,000, but this number is meaningless from ERPNExt POV
	customers_per_warehouse: int = 3000

	def __post_init__(self):
		frappe.flags.in_import = 1

	def setup_all(self):
		self.setup_custom_fields()
		self.setup_custom_naming()
		self.setup_company()
		self.setup_items()
		self.setup_warehouses()
		self.setup_customers()
		self.setup_users()
		self.setup_opening_stock()
		frappe.db.commit()
		frappe.cache.flushall()

	def setup_company(self):
		if frappe.db.exists("Company", COMPANY_NAME):
			frappe.throw("Company already exists, start again on a new site")

		current_year = now_datetime().year
		setup_complete(
			{
				"currency": "INR",
				"full_name": "Test User",
				"company_name": "The Company",
				"timezone": "Asia/Kolkata",
				"company_abbr": "TC",
				"industry": "Distribution",
				"country": "India",
				"fy_start_date": f"{current_year}-01-01",
				"fy_end_date": f"{current_year}-12-31",
				"language": "english",
				"company_tagline": "Testing",
				"email": "test@erpnext.com",
				"password": "test",
				"chart_of_accounts": "Standard",
			}
		)

		# just copied from ERPNext
		defaults = {
			"customer_group": "Individual",
			"territory": get_root_of("Territory"),
		}
		frappe.db.set_single_value("Selling Settings", defaults)
		for key, value in defaults.items():
			frappe.db.set_default(key, value)
		frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 0)

		frappe.db.commit()

	def setup_custom_fields(self):
		"""Add custom field on warehouses for naming."""
		create_custom_fields(
			{
				("Sales Invoice", "Delivery Note", "Payment Entry"): [
					{"fieldname": "branch", "fieldtype": "Data", "label": "Branch"}
				],
			}
		)

	def setup_custom_naming(self):
		"""Setup branch specific naming to minimize contention."""

		naming_series = {
			"Sales Invoice": "INV-.branch.-.######",
			"Delivery Note": "DN-.branch.-.######",
			"Payment Entry": "PE-.branch.-.######",
		}

		for doctype, series in naming_series.items():
			make_property_setter(doctype, "naming_series", "default", "", "Text")
			make_property_setter(doctype, "naming_series", "options", series, "Text")
			make_property_setter(doctype, "naming_series", "default", series, "Text")

	def setup_items(self):
		name = name_generator(ITEM_NAME, 6)
		template = frappe.new_doc("Item", is_stock_item=True, item_group=get_root_of("Item Group"))
		template.item_code = next(name)
		template.insert().reload()
		# Erase unnecessary defaults
		template.item_defaults = []
		template.uoms = []

		def item_generator():
			for _ in tqdm.tqdm(range(self.n_items - 1)):
				item = deepcopy(template)
				item.name = item.item_code = item.item_name = item.description = next(name)
				yield item

		bulk_insert("Item", item_generator(), chunk_size=1000, commit_chunks=True)

	def setup_warehouses(self):
		name = name_generator(WAREHOUSE_NAME, 4)

		parent_warehouse = get_root_of("Warehouse")
		for _ in tqdm.tqdm(range(self.n_warehouses)):
			warehouse = frappe.new_doc("Warehouse")
			warehouse.parent_warehouse = parent_warehouse
			warehouse.company = COMPANY_NAME
			warehouse.warehouse_name = warehouse.name = next(name)
			warehouse.insert()
		frappe.db.commit()

	def setup_customers(self):
		name = name_generator(CUSTOMER_NAME, 6)
		template = (
			frappe.new_doc(
				"Customer",
				customer_group="Individual",
				customer_name=next(name),
			)
			.insert()
			.reload()
		)

		def customer_generator():
			for _ in tqdm.tqdm(range(self.n_warehouses * self.customers_per_warehouse - 1)):
				customer = deepcopy(template)
				customer.name = customer.customer_name = next(name)
				yield customer

		bulk_insert("Customer", customer_generator(), chunk_size=1000, commit_chunks=True)

	def setup_users(self):
		name = name_generator(USER_NAME, 4)
		frappe.flags.in_import = 1

		all_roles = set(frappe.get_all("Role", pluck="name"))
		for _ in tqdm.tqdm(range(self.n_warehouses * self.users_per_warehouse)):
			user = frappe.new_doc("User")
			user.email = user.first_name = user.new_password = next(name)
			user.send_welcome_email = False
			user.flags.ignore_password_policy = True
			for role in all_roles - set(AUTOMATIC_ROLES):
				user.append("roles", {"role": role})
			if not frappe.db.exists("User", user.email):
				user.insert()

	def setup_opening_stock(self):
		items = frappe.get_all(
			"Item", {"name": ("like", "I-%")}, pluck="name", order_by="name", limit=self.n_items
		)
		warehouses = frappe.get_all("Warehouse", {"name": ("like", "WH-%")}, pluck="name")
		batches = list(itertools.batched(itertools.product(items, warehouses), 100))
		for batch in tqdm.tqdm(batches):
			sr: StockReconciliation = frappe.new_doc("Stock Reconciliation")
			sr.purpose = "Opening Stock"
			sr.expense_account = "Temporary Opening - TC"
			for item, warehouse in batch:
				sr.append(
					"items",
					{
						"item_code": item,
						"warehouse": warehouse,
						"qty": 10_000,
						"valuation_rate": 1.0,
					},
				)
			sr.insert()
			sr.submit()
			frappe.db.commit()


def name_generator(series: str, digits):
	i = 0
	while i := i + 1:
		yield series.format(str(i).zfill(digits))
