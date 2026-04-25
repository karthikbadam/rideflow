-- Source-of-truth tables for drivers and rides.
-- driver_id is TEXT (format 'drv-XXXX') so it matches the simulator's IDs
-- directly — the CDC-enrichment Flink job joins on string equality without
-- a Postgres-only UUID mapping view.
--
-- REPLICA IDENTITY FULL lets Debezium emit both before and after images on
-- UPDATEs, which matters for change-stream semantics in Flink (Phase 2.5).

CREATE TABLE IF NOT EXISTS drivers (
    driver_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT,
    vehicle_type  TEXT NOT NULL CHECK (vehicle_type IN ('economy', 'comfort', 'premium')),
    rating        NUMERIC(3, 2) DEFAULT 5.00,
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE drivers REPLICA IDENTITY FULL;

CREATE TABLE IF NOT EXISTS rides (
    ride_id       UUID PRIMARY KEY,
    rider_id      TEXT NOT NULL,
    driver_id     TEXT REFERENCES drivers(driver_id),
    state         TEXT NOT NULL DEFAULT 'requested'
                  CHECK (state IN ('requested','matched','picked_up','completed','cancelled')),
    pickup_lat    DOUBLE PRECISION NOT NULL,
    pickup_lon    DOUBLE PRECISION NOT NULL,
    dropoff_lat   DOUBLE PRECISION,
    dropoff_lon   DOUBLE PRECISION,
    fare          NUMERIC(10, 2),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE rides REPLICA IDENTITY FULL;

-- Publication scoped to the tables we expose to Debezium. The connector
-- config's publication.autocreate.mode=disabled tells it to use this one
-- rather than create its own wide-open publication.
CREATE PUBLICATION rideflow_cdc FOR TABLE drivers, rides;
