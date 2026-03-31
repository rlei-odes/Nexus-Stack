# =============================================================================
# Dinky - Custom image with Kafka SQL Connector
# =============================================================================
# The official Dinky image includes an embedded Flink runtime but does not
# ship the Kafka SQL connector. Without it, Dinky cannot execute Flink SQL
# statements that read from or write to Kafka/Redpanda topics.
#
# This Dockerfile downloads the flink-sql-connector-kafka fat JAR into
# Dinky's Flink extensions directory so it is available out of the box.
# =============================================================================

FROM dinkydocker/dinky-standalone-server:1.2.5-flink1.20

# Download the Kafka SQL connector into Dinky's embedded Flink extensions.
# Version 3.4.0-1.20 is compatible with the bundled Flink 1.20 runtime.
RUN curl -fSL https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar \
        -o /opt/dinky/extends/flink1.20/flink-sql-connector-kafka-3.4.0-1.20.jar
