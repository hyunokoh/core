REGISTRY  ?= 127.0.0.1:5050
TAG       ?= 0.1.0
# PLATFORMS may be a comma-separated list. For multi-arch (e.g.
# linux/amd64,linux/arm64) buildx must --push because docker cannot store fat
# manifests locally; single-arch builds --load into the host daemon.
PLATFORMS ?= linux/amd64
CONTEXT   := $(shell cd ../.. && pwd)

.PHONY: registry base all push multi-arch scan sbom catalog clean help

help:
	@echo "make registry     - start local docker registry on :5050"
	@echo "make base         - build zkcex-base:latest (single-arch, --load)"
	@echo "make all          - build all service images for PLATFORMS (default: linux/amd64)"
	@echo "make push         - build + push all to \$$(REGISTRY)"
	@echo "make multi-arch   - shortcut for PLATFORMS=linux/amd64,linux/arm64 build_all.sh"
	@echo "make scan         - trivy scan every image (requires trivy)"
	@echo "make sbom         - syft SBOM every image (requires syft) -> sboms/"
	@echo "make catalog      - curl /v2/_catalog"
	@echo "make clean        - tear down registry + base image + builder"

registry:
	bash init-registry.sh

base:
	docker buildx build \
	  --platform $(PLATFORMS) \
	  -t zkcex-base:latest \
	  -t $(REGISTRY)/zkcex-base:$(TAG) \
	  --build-arg SERVICE=base \
	  --load \
	  -f Dockerfile.base \
	  $(CONTEXT)

all: base
	@set -e; for f in Dockerfile.*; do \
	  [ "$$f" = "Dockerfile.base" ] && continue || true; \
	  svc="$${f#Dockerfile.}"; \
	  echo "==> $$svc"; \
	  docker buildx build \
	    --platform $(PLATFORMS) \
	    -t $(REGISTRY)/zkcex-$$svc:$(TAG) \
	    -t zkcex-$$svc:$(TAG) \
	    --build-arg "SERVICE=$$svc" \
	    --load \
	    -f "$$f" $(CONTEXT); \
	done

push: all
	@set -e; for f in Dockerfile.*; do \
	  [ "$$f" = "Dockerfile.base" ] && continue || true; \
	  svc="$${f#Dockerfile.}"; \
	  docker push $(REGISTRY)/zkcex-$$svc:$(TAG); \
	done

# Multi-arch path delegates to build_all.sh because buildx cannot mix --load
# and --platform=multi inside a single docker build call.
multi-arch:
	PLATFORMS=linux/amd64,linux/arm64 REGISTRY=$(REGISTRY) TAG=$(TAG) bash build_all.sh

scan:
	@command -v trivy >/dev/null || { echo "trivy not installed - brew install trivy"; exit 0; }
	@for f in Dockerfile.*; do \
	  [ "$$f" = "Dockerfile.base" ] && continue || true; \
	  svc="$${f#Dockerfile.}"; \
	  echo "==> trivy $$svc"; \
	  trivy image --severity HIGH,CRITICAL --no-progress $(REGISTRY)/zkcex-$$svc:$(TAG) || true; \
	done

sbom:
	@command -v syft >/dev/null || { echo "syft not installed - brew install syft"; exit 0; }
	@mkdir -p sboms
	@for f in Dockerfile.*; do \
	  [ "$$f" = "Dockerfile.base" ] && continue || true; \
	  svc="$${f#Dockerfile.}"; \
	  echo "==> syft $$svc"; \
	  syft $(REGISTRY)/zkcex-$$svc:$(TAG) -o spdx-json > sboms/$$svc.spdx.json 2>/dev/null || true; \
	done

catalog:
	@curl -s http://127.0.0.1:5050/v2/_catalog | python3 -m json.tool

clean:
	docker compose -f registry-compose.yml down -v
	docker buildx rm zkcex-builder 2>/dev/null || true
	docker rmi zkcex-base:latest 2>/dev/null || true
