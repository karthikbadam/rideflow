-- Seed drivers matching the simulator's driver_id namespace (drv-0000..drv-0049).
-- The CDC-enrichment Flink job joins ride.request.v1 against this table, so
-- any ride whose assigned driver has a row here can be enriched with name +
-- vehicle_type + rating + active.

INSERT INTO drivers (driver_id, name, phone, vehicle_type, rating, active) VALUES
  ('drv-0000', 'Alice Chen',    '+1-415-555-0001', 'economy', 4.92, true),
  ('drv-0001', 'Bruno Okafor',  '+1-415-555-0002', 'comfort', 4.85, true),
  ('drv-0002', 'Carmen Diaz',   '+1-415-555-0003', 'premium', 4.97, true),
  ('drv-0003', 'Dev Patel',     '+1-415-555-0004', 'economy', 4.71, true),
  ('drv-0004', 'Elena Rossi',   '+1-415-555-0005', 'comfort', 4.89, true),
  ('drv-0005', 'Farid Hassan',  '+1-415-555-0006', 'economy', 4.60, false),
  ('drv-0006', 'Grace Lin',     '+1-415-555-0007', 'premium', 4.99, true),
  ('drv-0007', 'Hiro Tanaka',   '+1-415-555-0008', 'comfort', 4.78, true),
  ('drv-0008', 'Isla Murphy',   '+1-415-555-0009', 'economy', 4.83, true),
  ('drv-0009', 'Jamal Wright',  '+1-415-555-0010', 'premium', 4.94, true),
  ('drv-0010', 'Kira Novak',    '+1-415-555-0011', 'economy', 4.65, true),
  ('drv-0011', 'Lukas Weber',   '+1-415-555-0012', 'comfort', 4.81, true),
  ('drv-0012', 'Maya Shah',     '+1-415-555-0013', 'premium', 4.96, true),
  ('drv-0013', 'Noel Park',     '+1-415-555-0014', 'economy', 4.72, true),
  ('drv-0014', 'Olga Ivanova',  '+1-415-555-0015', 'comfort', 4.88, true),
  ('drv-0015', 'Priya Rao',     '+1-415-555-0016', 'premium', 4.93, true),
  ('drv-0016', 'Quentin Allard','+1-415-555-0017', 'economy', 4.55, false),
  ('drv-0017', 'Ravi Singh',    '+1-415-555-0018', 'comfort', 4.79, true),
  ('drv-0018', 'Sara Kim',      '+1-415-555-0019', 'premium', 4.98, true),
  ('drv-0019', 'Tariq Jallow',  '+1-415-555-0020', 'economy', 4.68, true),
  ('drv-0020', 'Uma Das',       '+1-415-555-0021', 'comfort', 4.82, true),
  ('drv-0021', 'Vadim Petrov',  '+1-415-555-0022', 'premium', 4.95, true),
  ('drv-0022', 'Wendy Chou',    '+1-415-555-0023', 'economy', 4.73, true),
  ('drv-0023', 'Xander Moore',  '+1-415-555-0024', 'comfort', 4.86, true)
ON CONFLICT (driver_id) DO NOTHING;

-- A couple of seed rides so Debezium's snapshot has rides-table rows too.
INSERT INTO rides (ride_id, rider_id, driver_id, state, pickup_lat, pickup_lon, dropoff_lat, dropoff_lon, fare) VALUES
  ('11111111-1111-1111-1111-000000000001', 'rdr-00001', 'drv-0002', 'completed', 37.7749, -122.4194, 37.7946, -122.3999, 18.50),
  ('11111111-1111-1111-1111-000000000002', 'rdr-00002', 'drv-0007', 'picked_up', 37.7599, -122.4148, 37.8030, -122.4378, 24.00)
ON CONFLICT (ride_id) DO NOTHING;
