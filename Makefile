IMAGE_NAME ?= reversi-workshop
CONTAINER_NAME ?= reversi-workshop
HOST_PORT ?= 8000
CONTAINER_PORT ?= 8000
REVERSI_OPERATOR_PASSWORD ?=

.PHONY: build check-password start stop restart logs status

build:
	docker build -t $(IMAGE_NAME) .

check-password:
	@if [ -z "$(REVERSI_OPERATOR_PASSWORD)" ]; then \
		echo "REVERSI_OPERATOR_PASSWORD is required. Example:"; \
		echo "  make start REVERSI_OPERATOR_PASSWORD='choose-a-strong-password'"; \
		exit 1; \
	fi

start: check-password build stop
	docker run -d \
		--name $(CONTAINER_NAME) \
		-p $(HOST_PORT):$(CONTAINER_PORT) \
		-e REVERSI_OPERATOR_PASSWORD="$(REVERSI_OPERATOR_PASSWORD)" \
		-e REVERSI_HOST=0.0.0.0 \
		-e REVERSI_PORT=$(CONTAINER_PORT) \
		$(IMAGE_NAME)
	@echo "Reversi is starting on http://127.0.0.1:$(HOST_PORT)"

stop:
	@docker rm -f $(CONTAINER_NAME) >/dev/null 2>&1 || true

restart: stop start

logs:
	docker logs -f $(CONTAINER_NAME)

status:
	docker ps -a --filter name=^/$(CONTAINER_NAME)$$

test:
	@echo "Running tests..."
	@python3 -m unittest discover -s tests -p "*.py" -v

