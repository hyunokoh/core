ARG CONFLUENT_PLATFORM_VERSION=7.6.1
FROM confluentinc/cp-kafka:${CONFLUENT_PLATFORM_VERSION}
USER root
RUN mkdir /opt/prometheus
RUN chmod +rx /opt/prometheus
COPY jmx-exporter-0.16.1.jar /opt/prometheus/jmx-exporter.jar
COPY kafka-jmx-exporter.yml /opt/prometheus
