FROM confluentinc/cp-kafka-connect:7.6.1

# Debezium PG source — Phase 2.5 CDC. 2.5.x matches Confluent 7.6 classpath
# (Kafka 3.6) without the message.converter compatibility shims that later 3.x
# Debezium needs. confluent-hub installs into /usr/share/confluent-hub-components,
# which is already on CONNECT_PLUGIN_PATH.
RUN confluent-hub install --no-prompt debezium/debezium-connector-postgresql:2.5.4
