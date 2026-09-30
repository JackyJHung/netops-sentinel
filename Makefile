.PHONY: install test lint run eval eval-tune serve docker

install:
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests scripts

run:
	sentinel run --seed 42 -v

eval:
	sentinel eval --split test --out reports

eval-tune:
	sentinel eval --split tune --out reports

serve:
	sentinel serve --port 8000

docker:
	docker build -t netops-sentinel . && docker run --rm -p 8000:8000 netops-sentinel
