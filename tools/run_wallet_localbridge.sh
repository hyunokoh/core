#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/hoh/Documents/Projects/zkCEX/core"
JAVA_HOME="${ROOT}/.local-tools/jdk-21.0.11.jdk/Contents/Home"
MAVEN_BIN="${ROOT}/.local-tools/apache-maven-3.9.9/bin"

export JAVA_HOME
export PATH="${MAVEN_BIN}:${JAVA_HOME}/bin:${PATH}"

cd "${ROOT}"

mvn -pl wallet/wallet-app -am -DskipTests package

exec java \
  -Dspring.cloud.bootstrap.enabled=false \
  -Dspring.config.import= \
  -Dspring.cloud.vault.enabled=false \
  -Dspring.cloud.vault.fail-fast=false \
  -Ddbusername=opex \
  -Ddbpassword=hiopex \
  -DDB_IP_PORT=localhost:5435 \
  -jar "${ROOT}/wallet/wallet-app/target/wallet-app.jar" \
  --spring.profiles.active=localbridge
