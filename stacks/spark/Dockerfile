# =============================================================================
# Apache Spark - Custom image with Python 3.13 and S3A support
# =============================================================================
# The official apache/spark:4.1.1 image is based on Ubuntu 22.04 (Jammy) which
# ships Python 3.10. Jupyter pyspark-notebook uses Python 3.13 (via conda).
# PySpark requires matching Python minor versions between driver and executors.
#
# This Dockerfile:
# 1. Installs Python 3.13 from the deadsnakes PPA to match Jupyter.
# 2. Downloads hadoop-aws + AWS SDK v2 JARs for S3A filesystem support
#    (required for s3a:// paths, e.g. Hetzner Object Storage).
# =============================================================================

FROM apache/spark:4.1.1

USER root

# Install Python 3.13 from deadsnakes PPA to match Jupyter's Python version.
# The base image (Ubuntu 22.04 Jammy) only ships Python 3.10.
RUN apt-get update && \
    apt-get install -y software-properties-common curl && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.13 && \
    rm -rf /var/lib/apt/lists/*

# Repoint /usr/bin/python3 -> python3.13. Spark 4.1.1's
# PythonWorkerFactory hardcodes the literal string "python3" when
# launching the executor's pyspark daemon, ignoring both the
# `spark.pyspark.python` SparkConf, the `spark.executorEnv.PYSPARK_PYTHON`
# config, and the `PYSPARK_PYTHON` env var that ARE all correctly set
# at task time. The daemon's `/proc/PID/exe` was confirmed live to
# resolve through the symlink. Net result: cluster-mode Jupyter-driver
# (3.13) + cluster-mode executor (3.10 via symlink) → PYTHON_VERSION_MISMATCH
# on every UDF / Python-touching task. Repointing the symlink at image
# build time is the only mechanism that bypasses that hardcode.
#
# Safety: the spark-worker container is minimal at runtime — no
# package installs (apt-get only runs at image build time, never
# inside the running container), no systemd, no OS scripts that
# would care which python3.x is the default. The previous concern
# about "not overwriting system python3" was theoretical; in
# practice nothing else in this image touches python3 at runtime
# except Spark itself, and Spark wants 3.13.
RUN ln -sf /usr/bin/python3.13 /usr/bin/python3

# PYSPARK_PYTHON env-var kept as belt-and-suspenders. With the symlink
# fix above, even Spark's hardcoded "python3" path resolves to 3.13,
# but explicit configuration still helps if anything in Spark looks
# up via this env var first.
ENV PYSPARK_PYTHON=/usr/bin/python3.13 \
    PYSPARK_DRIVER_PYTHON=/usr/bin/python3.13

# Download hadoop-aws and AWS SDK v2 bundle for S3A filesystem support.
# Spark 4.1.x ships Hadoop 3.4.2 but does not include the hadoop-aws module.
# These JARs enable s3a:// paths (e.g., Hetzner Object Storage).
RUN curl -fSL https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.4.2/hadoop-aws-3.4.2.jar \
        -o /opt/spark/jars/hadoop-aws-3.4.2.jar && \
    curl -fSL https://repo1.maven.org/maven2/software/amazon/awssdk/bundle/2.29.52/bundle-2.29.52.jar \
        -o /opt/spark/jars/bundle-2.29.52.jar

USER spark
