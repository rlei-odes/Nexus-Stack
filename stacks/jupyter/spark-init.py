import os
try:
    from IPython import get_ipython
    from pyspark.sql import SparkSession
    master = os.environ.get("SPARK_MASTER", "local[*]")
    builder = SparkSession.builder.master(master).appName("Jupyter Notebook")
    # Pin executor Python to 3.13 ONLY in cluster mode. The
    # spark-worker container has /usr/bin/python3.13 installed
    # (custom Dockerfile), but the default symlink /usr/bin/python3
    # still points at 3.10 — without this, the executor forks
    # python3 → 3.10 → PYTHON_VERSION_MISMATCH against Jupyter's
    # 3.13 driver. In local mode the "executor" runs in this same
    # container (which has its python at /opt/conda/bin/python via
    # conda, not /usr/bin/python3.13), so we leave Spark to pick up
    # its own interpreter.
    #
    # We set BOTH `spark.pyspark.python` and
    # `spark.executorEnv.PYSPARK_PYTHON` because Spark 4.1.1
    # standalone-cluster PythonWorkerFactory ignores the former in
    # some code paths and falls back to the literal string "python3"
    # — observed live: config showed
    # `spark.pyspark.python=/usr/bin/python3.13` correctly via
    # /api/v1/applications/.../environment, yet the executor still
    # spawned `python3 -m pyspark.daemon` (= 3.10 via the OS
    # symlink), producing PYTHON_VERSION_MISMATCH. The
    # `spark.executorEnv.*` config family is Spark's explicit
    # mechanism for propagating env vars to executor processes,
    # which the worker factory consults directly via envVars
    # before any conf lookup. Belt + suspenders.
    if master.startswith("spark://"):
        executor_python = "/usr/bin/python3.13"
        builder = (builder
                   .config("spark.pyspark.python", executor_python)
                   .config("spark.executorEnv.PYSPARK_PYTHON", executor_python))
    endpoint = os.environ.get("SPARK_HADOOP_fs_s3a_endpoint", "")
    if endpoint:
        builder = builder \
            .config("spark.hadoop.fs.s3a.endpoint", endpoint) \
            .config("spark.hadoop.fs.s3a.access.key", os.environ.get("SPARK_HADOOP_fs_s3a_access_key", "")) \
            .config("spark.hadoop.fs.s3a.secret.key", os.environ.get("SPARK_HADOOP_fs_s3a_secret_key", "")) \
            .config("spark.hadoop.fs.s3a.path.style.access", "true") \
            .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")
    _spark = builder.getOrCreate()
    _sc = _spark.sparkContext
    # Inject into notebook namespace so spark/sc are available in cells
    _ip = get_ipython()
    _ip.user_ns["spark"] = _spark
    _ip.user_ns["sc"] = _sc
    print(f"SparkSession ready (master: {master})")
    del _spark, _sc, _ip
except Exception as e:
    print(f"Spark not available: {e}")
