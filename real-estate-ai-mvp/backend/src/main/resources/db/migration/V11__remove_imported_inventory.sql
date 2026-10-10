-- The File management -> Projects & inventory import was withdrawn. Remove what it created: units
-- that came from files (data.importKey) and projects the import made (data.importCreated). Rows that
-- reference them (status history, prices, recommendations, ...) go first. Projects and units created
-- in the CRM are not touched. If something unexpected still references an imported row, the delete
-- is undone and the rows are hidden instead (project INACTIVE, unit UNAVAILABLE), so startup never fails.
DO $$
DECLARE ref record; n bigint;
BEGIN
  CREATE TEMP TABLE gone_units ON COMMIT DROP AS SELECT id FROM units WHERE data->>'importKey' IS NOT NULL;
  CREATE TEMP TABLE gone_projects ON COMMIT DROP AS SELECT id FROM projects WHERE data->>'importCreated' = 'true';
  BEGIN
    FOR ref IN
      SELECT DISTINCT cl.relname AS tbl, a.attname AS col, ft.relname AS target
      FROM pg_constraint k
      JOIN pg_class cl ON cl.oid = k.conrelid
      JOIN pg_class ft ON ft.oid = k.confrelid
      JOIN LATERAL unnest(k.conkey) AS c(n) ON true
      JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = c.n
      WHERE k.contype = 'f' AND ft.relname IN ('units', 'projects')
        AND a.attname IN ('unit_id', 'project_id') AND cl.relname NOT IN ('units', 'projects')
      ORDER BY 3 DESC  -- references to units before references to projects
    LOOP
      EXECUTE format('DELETE FROM %I WHERE %I IN (SELECT id FROM %I)', ref.tbl, ref.col,
                     CASE WHEN ref.target = 'units' THEN 'gone_units' ELSE 'gone_projects' END);
    END LOOP;
    -- Support tables with a plain project_id / unit_id column and no foreign key.
    DELETE FROM unit_status_history WHERE unit_id IN (SELECT id FROM gone_units);
    DELETE FROM unit_prices WHERE unit_id IN (SELECT id FROM gone_units);
    DELETE FROM project_locations WHERE project_id IN (SELECT id FROM gone_projects);
    DELETE FROM project_amenities WHERE project_id IN (SELECT id FROM gone_projects);
    DELETE FROM units WHERE id IN (SELECT id FROM gone_units);
    DELETE FROM units WHERE project_id IN (SELECT id FROM gone_projects);
    DELETE FROM projects WHERE id IN (SELECT id FROM gone_projects);
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE 'removed % imported project(s)', n;
  EXCEPTION WHEN others THEN
    RAISE NOTICE 'imported rows are still referenced (%); hiding them instead', SQLERRM;
    UPDATE units SET status = 'UNAVAILABLE', updated_at = now() WHERE id IN (SELECT id FROM gone_units);
    UPDATE projects SET status = 'INACTIVE', updated_at = now() WHERE id IN (SELECT id FROM gone_projects);
  END;
  DELETE FROM knowledge_imports;
END $$;
