FROM flink:1.18.1-scala_2.12-java11

USER root

# Python 3 + build toolchain. The base image ships a JRE, but `pemja` (a
# PyFlink dep, pinned at 0.3.0 with no prebuilt aarch64 wheel) compiles a
# native extension and needs JDK headers at /opt/java/openjdk/include.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev curl ca-certificates \
        openjdk-11-jdk-headless build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python3 /usr/bin/python && \
    ln -s /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)/include /opt/java/openjdk/include

# PyFlink + job-runtime deps. PyFlink wheel is ~300MB; --no-cache-dir keeps
# the final image slimmer.
RUN pip3 install --no-cache-dir \
        apache-flink==1.18.1 \
        redis==5.0.1 \
        fastavro==1.9.7 \
        requests==2.32.3

# Connector JARs:
#   - flink-sql-connector-kafka: Kafka source (fat JAR with kafka-clients)
#   - flink-sql-avro-confluent-registry: avro-confluent format handling SR
ARG FLINK_KAFKA_CONN=flink-sql-connector-kafka-3.1.0-1.18.jar
ARG FLINK_AVRO_CONFLUENT=flink-sql-avro-confluent-registry-1.18.1.jar
RUN curl -fsSL -o /opt/flink/lib/${FLINK_KAFKA_CONN} \
        https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.1.0-1.18/${FLINK_KAFKA_CONN} && \
    curl -fsSL -o /opt/flink/lib/${FLINK_AVRO_CONFLUENT} \
        https://repo1.maven.org/maven2/org/apache/flink/flink-sql-avro-confluent-registry/1.18.1/${FLINK_AVRO_CONFLUENT}

COPY --chown=flink:flink jobs /opt/flink/jobs

USER flink
