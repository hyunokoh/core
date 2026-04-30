# zkCEX Exchange Test Hardening Brief

## Goal
Replace mock-heavy exchange tests with real integration coverage for OPEX exchange flows. zkPoL/zkAML work is lower priority for this phase; the priority is proving the exchange works end-to-end.

## Non-negotiables
- Prefer real implementations over `mockk`, Mockito `@MockBean`, MockServer, and Spring Cloud Stream test binder.
- Use Testcontainers or local Docker Compose services for Kafka/Postgres-backed tests.
- Keep upstream OPEX untouched; work only in this fork.
- Do not remove existing zk integration code unless it blocks exchange tests.

## Current blockers
- App-level tests for `accountant-app` and `wallet-app` currently fail before logic runs because old Testcontainers cannot validate Docker Desktop/Docker 29.
- Existing tests in core/persister modules are mostly unit tests with mocks and should be replaced or supplemented with real repositories/services.

## Desired implementation path
1. Make Testcontainers work reliably with current Docker Desktop.
2. Run `accountant-app` and `wallet-app` tests with real Kafka and Testcontainers Postgres.
3. Remove in-memory Spring Cloud Stream test binder from app integration tests.
4. Replace Mockito/MockK app test seams with real beans where feasible.
5. Add a real exchange smoke test that covers wallet balance setup, order submission, matching, accountant financial actions, and market visibility.

## Verification command
Use the local JDK/Maven and project-local Maven repo:

```sh
env JAVA_HOME=/Users/hoh/Documents/Projects/zkCEX/core/.local-tools/jdk-21.0.11.jdk/Contents/Home \
PATH=/Users/hoh/Documents/Projects/zkCEX/core/.local-tools/jdk-21.0.11.jdk/Contents/Home/bin:/Users/hoh/Documents/Projects/zkCEX/core/.local-tools/apache-maven-3.9.9/bin:/usr/bin:/bin:/usr/sbin:/sbin \
DOCKER_HOST=unix:///Users/hoh/.docker/run/docker.sock \
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock \
.local-tools/apache-maven-3.9.9/bin/mvn \
-Dmaven.repo.local=/Users/hoh/Documents/Projects/zkCEX/core/.m2/repository \
-Dskip.unit.tests=false \
-pl accountant/accountant-app,wallet/wallet-app -am test
```
