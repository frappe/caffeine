### Caffeine

Frappes are usually not caffeinated enough, we want a lot of caffeine in ours.

[![Sanity Test](https://github.com/frappe/caffeine/actions/workflows/ci.yml/badge.svg?event=schedule)](https://github.com/frappe/caffeine/actions/workflows/ci.yml)

### Goal

This project has only one goal: **Speedup Frappe ecosystem by approximately by 2x.**

Approximately, this boils down to:
- Set up a good benchmark suite ranging from microbenchmarks to realistic traces.
- Optimize EVERYTHING. Every 0.1% on critical path counts.
- Make deployments resource efficient by tuning various knobs.

### Microbenchmarks

Microbenchmarks are now part of Frappe Framework.
Refer to [`frappe/tests/microbenchmarks`](https://github.com/frappe/frappe/tree/develop/frappe/tests/microbenchmarks) for the steps to run and write them.


### E2E load testing

This repo contains a TPC-C inspired benchmark and load testing script for ERPNext.

#### Pre-requisites

- New site with ERPNext installed.
- Grafana K6 - [Installation Guide](https://grafana.com/docs/k6/latest/set-up/install-k6/)
- Basic understanding of how Frappe, ERPNext, and K6 work.

#### Usage

We use k6 to generate load. You can read their [documentation](https://grafana.com/docs/k6/latest/get-started/) to get started.

```bash
# Setup dummy master data for load testing
bench --site test_site setup-loadtest-data
# Run the load test with 100 virtual concurrent users
k6 run --vus 100 --duration 10m -e BASE_URL=http://sitename:port ./apps/caffeine/caffeine/loadtest/script.js
```

Other k6 variables: `THINK_TIME` multiplies the pauses between steps (`0` turns them off), `ITEMS_PER_INVOICE` sets the item rows per invoice (default 3), and `SITE` sends the site name as a header, so `BASE_URL` can be an IP address.

Note: fixture records have hard-coded naming to simplify writing tests without sending too many arguments. We intend to make it flexible over time if required.

#### Workload

The workload is inspired by [TPC-C](https://www.tpc.org/tpc_documents_current_versions/pdf/tpc-c_v5.11.0.pdf), a standard benchmark for order-processing systems.
TPC-C models a wholesale supplier with warehouses, customers, items and stock. Users enter orders, record payments and deliver orders.
ERPNext does the same work with Sales Invoice, Payment Entry and Delivery Note.

**Master data**

`bench setup-loadtest-data` creates the master data with bulk inserts. The default values are:

| Data      | Quantity                    | TPC-C equivalent                                   |
| --------- | --------------------------- | -------------------------------------------------- |
| Warehouse | 10                          | Warehouse. This is the scaling factor.             |
| User      | 10 per warehouse (100 total) | Terminal                                          |
| Customer  | 3000 per warehouse          | Customer (TPC-C uses 30,000 per warehouse)         |
| Item      | 1000                        | Item (TPC-C uses 100,000)                          |
| Stock     | 10,000 units of each item in each warehouse | Stock                              |

Each warehouse is a "branch" with its own naming series for invoices, payments and deliveries. This prevents contention on one shared series counter, as TPC-C does with its per-district order numbers.

**Transactions**

Each user is one k6 virtual user, and it logs in as its own ERPNext user. Each virtual user runs this sequence in a loop:

| Step | Requests                                                        | TPC-C equivalent |
| ---- | --------------------------------------------------------------- | ---------------- |
| 1    | Ping the server.                                                | -                |
| 2    | Load the Sales Invoice list view and its count.                 | -                |
| 3    | Create a Sales Invoice with 3 random items for a random customer, from the user's warehouse. Read it again. | New-Order |
| 4    | Submit the Sales Invoice and read it again.                     | New-Order        |
| 5    | Make, save and submit a Payment Entry. Read the invoice again to check that its status is "Paid". | Payment, Order-Status |
| 6    | Make, save and submit a Delivery Note. Read the invoice again.  | Delivery         |

Each request goes through the same API that the desk UI uses.
Each user waits a random time of 0-2 seconds between steps, and 0.5 seconds between requests in steps 5 and 6.
This think time is short. It is similar to an API integration, not to a human user.

Differences from TPC-C:

- TPC-C runs a random mix of transactions. This script runs one fixed sequence.
- This script does not have an equivalent of the TPC-C Stock-Level transaction.
- The write-to-read ratio is high. Most sites that people use directly have approximately 1 write for each 10 reads.

#### Results

We ran the test on Frappe and ERPNext v16 (develop branch, June 2025), on a Frappe Cloud server:

- VM: AWS EC2 `c6i.4xlarge` (Intel Xeon, 3rd generation), in the `ap-south-1` (Mumbai) region
- CPU: 16 vCPU
- RAM: 32 GB

A "transaction" is one submitted document: a Sales Invoice, a Payment Entry or a Delivery Note.
Each transaction needs 4 requests: 2 writes (save and submit) and 2 reads.

- Sales Invoice: save, read the invoice, submit, read the invoice.
- Payment Entry: make a new payment from the invoice, save, submit, read the invoice.
- Delivery Note: make a new delivery from the invoice, save, submit, read the invoice.

Each loop of the sequence gives 3 transactions. Each loop also sends 3 more requests: a ping and the 2 list view requests. Thus, each loop sends 15 requests.

The ~130K transactions/hour result comes from a 2-hour soak test with 100 virtual users.

| Metric                                  | Value             |
| --------------------------------------- | ----------------- |
| Peak throughput (latency not limited)   | ~150K transactions/hour |
| Throughput with P90 latency below 1s    | ~130K transactions/hour |
| Speedup from the start of this project  | ~2.2x             |

These numbers apply only to this workload and this hardware. Use them to estimate the order of magnitude only.
If you need better estimates, write a workload that matches your use case and test it on your hardware.

#### Profiled runs and regression diffs

`caffeine/loadtest/harness/lt.py` runs the same workload with measurements, so you can compare two builds. It starts its own gunicorn, job worker and scheduler for the site, so stop the bench's own web and worker processes first.

- `load`: k6 with py-spy on the web and worker processes. It also records database and redis counters, the slow log, row-lock waits, the latest InnoDB deadlock, Error Log entries and the host load.
- `profile`: one user and the recorder, at 1 and 11 items per invoice. It gives the queries per request and per item row, with their call sites.
- `compare`: lists the regressions between two runs first, then the improvements.

```bash
./env/bin/pip install py-spy
H=./apps/caffeine/caffeine/loadtest/harness/lt.py
# Save the baseline that `--reset` restores before each run
./env/bin/python $H reset --bench . --site test_site --save
./env/bin/python $H profile --bench . --site test_site --label queries-base --reset
./env/bin/python $H load --bench . --site test_site --label load-base --reset --duration 10m --vus 20
# Switch the apps to the new build, run the same two commands with new labels, then:
./env/bin/python $H compare logs/loadtest/<base run> logs/loadtest/<new run>
```

Runs go to `logs/loadtest/` in the bench. Each run has `summary.md`, `summary.json` and the raw files. Use `analyze` to rebuild a summary from the raw files.

Environment variables:

| Variable | Use |
| --- | --- |
| `LT_DB_ROOT_PASSWORD`, `LT_DB_ROOT_USER` | Database admin login. It is necessary for the slow log, lock waits, the deadlock report and Postgres resets. Without it, the harness uses the site's own user and records only the server counters. |
| `LT_DB_LOG` | Path of the database server log. The harness copies the part written during the run. Postgres slow statements and lock waits are read from this copy. |
| `K6` | Path of the k6 binary, if it is not on `PATH`. |

The MariaDB slow log is written by the database server into the run directory, so it works only when the database runs on the same host.

#### Tweaking / Extending

If you want to write custom benchmarks, consider forking the repository or creating a new script.
The goal of this project, as of now, is to just provide the boilerplate required for writing such tests.

The following variations are worth exploring in the future:

- Realistic think times. This repo has an aggressive high-throughput API ingestion scenario. If your service will only be used by humans, then you can relax the think times.
- Read heavy workloads. This repo has a disproportionately high W:R ratio. Most sites used by end users directly will have 1:10 write-to-read ratio.
- Different modules and doctypes. Currently, we just do sales invoice -> payment -> Delivery simulation.
- Randomness / Markov modelling on top of end-user traces / complex workflows


### Contributing

At present, this repo is not accepting any external contributions.

### License

AGPL-3.0
