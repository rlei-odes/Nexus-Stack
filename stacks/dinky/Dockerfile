# =============================================================================
# Dinky - Custom image with Kafka SQL Connector and persistent catalog
# =============================================================================
# The official Dinky standalone image (H2 mode) is missing:
# 1. The Kafka SQL connector — needed for Flink SQL with Redpanda/Kafka
# 2. The MySQL JDBC driver — needed for Dinky's built-in catalog
#    (the catalog uses MySQL-compatible SQL even in H2 mode, but the
#    class loader check requires the MySQL driver to be present)
#
# With both JARs added, "DefaultCatalog" works in Dinky and table
# definitions persist across tasks (no need to repeat CREATE TABLE).
# =============================================================================

FROM dinkydocker/dinky-standalone-server:1.2.5-flink1.20

# Download the Kafka SQL connector into Dinky's embedded Flink extensions.
# Version 3.4.0-1.20 is compatible with the bundled Flink 1.20 runtime.
RUN curl -fSL https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar \
        -o /opt/dinky/extends/flink1.20/flink-sql-connector-kafka-3.4.0-1.20.jar

# Download MySQL JDBC driver for Dinky's built-in catalog persistence.
# The DinkyMysqlCatalog class requires this driver even in H2 standalone mode.
RUN curl -fSL https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.3.0/mysql-connector-j-8.3.0.jar \
        -o /opt/dinky/lib/mysql-connector-j-8.3.0.jar
