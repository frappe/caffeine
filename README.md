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

Note: fixture records have hard-coded naming to simplify writing tests without sending too many arguments. We intend to make it flexible over time if required.

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
