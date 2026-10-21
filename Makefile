.PHONY: setup doctor test run serve clean
setup:
	./setup.sh
doctor:
	.venv/bin/mc-harness doctor --config configs/local.yaml
test:
	.venv/bin/python -m unittest discover -s tests -q
	npm --prefix bot_runtime test
run:
	./scripts/run.sh
serve:
	.venv/bin/mc-harness serve runs --port 8896
clean:
	rm -rf runs/* validation/* .venv
