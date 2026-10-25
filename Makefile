# Everything runs in containers; nothing is installed on the host.

IMAGE      := bitx-gps-station-build
DOCKER     := docker run --rm -v "$(CURDIR)":/src -w /src
RUN        := $(DOCKER) $(IMAGE)
MDLINT     := davidanson/markdownlint-cli2:v0.24.0
ACTIONLINT := rhysd/actionlint:1.7.4
VERSION    := $(shell sed -n '1s/.*(\(.*\)).*/\1/p' debian/changelog)

.PHONY: all image lint lint-python lint-shell lint-markdown lint-actions \
	test deb repo hooks clean version

all: lint test deb

image:
	docker build -q -t $(IMAGE) docker

lint: lint-python lint-shell lint-markdown lint-actions

lint-python: image
	$(RUN) ruff check src tests

lint-shell: image
	$(RUN) shellcheck .githooks/pre-commit scripts/*.sh debian/*.postrm

lint-markdown:
	$(DOCKER) $(MDLINT) "**/*.md" "#.pytest_cache"

lint-actions:
	$(DOCKER) $(ACTIONLINT) -color

test: image
	$(RUN) python3 -m pytest -q tests

# Builds dist/gps-station_$(VERSION)_all.deb. Architecture "all": the same
# package installs on 32-bit (armhf) and 64-bit (arm64) Raspberry Pi OS.
deb: image
	$(RUN) sh -c 'rm -rf /tmp/b && mkdir /tmp/b \
		&& cp -a /src /tmp/b/gps-station \
		&& cd /tmp/b/gps-station && rm -rf dist .git \
		&& dpkg-buildpackage -us -uc -b \
		&& lintian --fail-on error ../*.deb \
		&& mkdir -p /src/dist && cp ../*.deb /src/dist/'

# Builds the apt repository tree in public/ from the .debs in dist/.
# Signs it when APT_SIGNING_KEY holds an armored private key.
repo: image
	$(DOCKER) -e APT_SIGNING_KEY $(IMAGE) scripts/build-apt-repo.sh dist public

hooks:
	git config core.hooksPath .githooks

version:
	@echo $(VERSION)

clean:
	rm -rf dist public .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
