GITHUB_USER = kbialek
VERSION = 2023.03.4

ARCHS = linux/amd64 linux/arm/v7 linux/arm64/v8

null =
space = $(null) $(null)
comma = ,

# Gets Github personal token from Bitwarden vault
get_github_token = \
	$(shell bw --session "$$(bw-read-session)" get item aa3dab0a-6c68-49d1-8a4d-193f37d3a5fc |\
		jq -r '.fields[] | select(.name=="token") | .value')

test:
	python -m unittest discover -p "*_test.py"

test-mqtt:
	python -m unittest "deye_mqtt_inttest.py"

run:
	@bash -c "$$(cat config.env | xargs) python deye_docker_entrypoint.py"

$(ARCHS:%=docker-build-%): docker-build-%:
	@docker buildx create --use
	@docker buildx build \
		--platform $* \
		--output type=docker \
		-t deye-inverter-mqtt:$(VERSION) \
		-t deye-inverter-mqtt:latest \
		.
	@docker buildx rm --all-inactive --force

docker-build-local: docker-build-linux/amd64

docker-run:
	@docker run --rm --env-file config.env deye-inverter-mqtt

docker-push: test
	@echo $(call get_github_token) | docker login ghcr.io -u $(GITHUB_USER) --password-stdin
	@docker buildx create --use
	@docker buildx build \
		--platform $(subst $(space),$(comma),$(ARCHS)) \
		--push \
		-t ghcr.io/$(GITHUB_USER)/deye-inverter-mqtt:$(VERSION) \
		-t ghcr.io/$(GITHUB_USER)/deye-inverter-mqtt:latest \
		.
	@docker buildx rm --all-inactive --force

METRIC_GROUPS = string micro deye_sg04lp3 deye_sg04lp3_battery
GENERATE_DOCS_TARGETS = $(addprefix generate-docs-, $(METRIC_GROUPS))
$(GENERATE_DOCS_TARGETS): generate-docs-%:
	@mkdir -p docs
	@cd tools && python metric_group_doc_gen.py --group-name=$* > ../docs/metric_group_$*.md

generate-all-docs: $(GENERATE_DOCS_TARGETS)