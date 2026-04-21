.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE := docker compose

.PHONY: help up down reset logs ps smoke register-schemas deregister-schemas submit-hot-zones submit-surge submit-idle submit-matching submit-phase2 topics shell-kafka shell-redis shell-pg list-jobs cancel-job

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Start the stack (builds images if needed)
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

reset: ## Full teardown including volumes
	bash scripts/reset.sh

logs: ## Tail logs for all services (use: make logs SVC=ingest-api for one)
	@if [ -z "$(SVC)" ]; then $(COMPOSE) logs -f --tail=100; else $(COMPOSE) logs -f --tail=200 $(SVC); fi

ps: ## Show service status
	$(COMPOSE) ps

smoke: ## End-to-end sanity check (Phase 0.8)
	bash scripts/smoke.sh

register-schemas: ## POST all Avro schemas to Schema Registry
	bash schemas/register.sh

deregister-schemas: ## DELETE all subjects (for compatibility experiments)
	bash schemas/deregister.sh

submit-hot-zones: ## Submit the hot-zones Flink job (detached)
	$(COMPOSE) exec -T flink-jm flink run -d \
	    --pyExecutable /usr/bin/python3 \
	    -py /opt/flink/jobs/01_hot_zones.py

submit-surge: ## Submit the surge-pricing Flink job (Phase 2.1)
	$(COMPOSE) exec -T flink-jm flink run -d \
	    --pyExecutable /usr/bin/python3 \
	    -py /opt/flink/jobs/02_surge_pricing.py

submit-idle: ## Submit the idle-detector Flink job (Phase 2.2)
	$(COMPOSE) exec -T flink-jm flink run -d \
	    --pyExecutable /usr/bin/python3 \
	    -py /opt/flink/jobs/03_idle_detector.py

submit-matching: ## Submit the ride-matching Flink job (Phase 2.3)
	$(COMPOSE) exec -T flink-jm flink run -d \
	    --pyExecutable /usr/bin/python3 \
	    -py /opt/flink/jobs/04_ride_matching.py

submit-phase2: submit-surge submit-idle submit-matching ## Submit all Phase 2 jobs

list-jobs: ## List currently running Flink jobs
	$(COMPOSE) exec -T flink-jm flink list

cancel-job: ## Cancel a Flink job: make cancel-job JID=<job-id>
	$(COMPOSE) exec -T flink-jm flink cancel $(JID)

topics: ## List Kafka topics with partition info
	bash scripts/tools.sh topics-list

shell-kafka: ## Exec into kafka-0 for CLI work
	$(COMPOSE) exec kafka-0 bash

shell-redis: ## redis-cli
	$(COMPOSE) exec redis redis-cli

shell-pg: ## psql as rideflow user
	$(COMPOSE) exec postgres psql -U rideflow -d rideflow
