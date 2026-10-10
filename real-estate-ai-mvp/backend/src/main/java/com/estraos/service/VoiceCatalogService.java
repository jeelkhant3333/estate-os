package com.estraos.service;

import com.estraos.exception.ApiException;
import com.estraos.security.TenantContext;
import java.math.BigDecimal;
import java.util.*;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * Read-only inventory lookups shaped for the voice agent.
 *
 * <p>These run while a customer is waiting on the phone, so each one is a single query returning
 * exactly what the agent speaks: no N+1 fan-out, no fields the agent discards. Only ACTIVE
 * projects and AVAILABLE units are ever returned — the agent must never quote something sold.
 */
@Service
public class VoiceCatalogService {
  private final NamedParameterJdbcTemplate db;
  private final TenantContext tenant;

  public VoiceCatalogService(NamedParameterJdbcTemplate db, TenantContext tenant) {
    this.db = db;
    this.tenant = tenant;
  }

  private static String text(Object value) {
    return value == null ? null : value.toString();
  }

  /** "Wakad, Pune" -> "loc_wakad", so the agent has a stable locality identifier. */
  static String localitySlug(String name) {
    String base = name == null ? "" : name.split(",")[0].trim().toLowerCase(Locale.ROOT);
    base = base.replaceAll("[^a-z0-9]+", "_").replaceAll("^_|_$", "");
    return base.isBlank() ? "loc_unknown" : "loc_" + base;
  }

  static String localityName(String location) {
    String name = location == null ? "" : location.split(",")[0].trim();
    return name.isBlank() ? "Unknown" : name;
  }

  /**
   * The whole speakable catalogue in one response.
   *
   * <p>The agent resolves what a caller says — a project name, a locality — against this, so it
   * has to hold every project and locality the workspace sells, not just the ones matching a
   * query. It is fetched at startup and refreshed periodically rather than per turn.
   */
  public Map<String, Object> catalog(Long ws) {
    tenant.require(ws);

    // Aggregate available stock per project and configuration in one pass.
    Map<Long, List<Map<String, Object>>> unitTypes = new LinkedHashMap<>();
    db.query(
        "SELECT project_id, bhk, min(area) AS carpet_area, min(price) AS price_min,"
            + " max(price) AS price_max, count(*) AS available"
            + " FROM units WHERE workspace_id=:ws AND status='AVAILABLE'"
            + " GROUP BY project_id, bhk ORDER BY project_id, bhk",
        Map.of("ws", ws),
        rs -> {
          long projectId = rs.getLong("project_id");
          Map<String, Object> type = new LinkedHashMap<>();
          type.put("id", "ut_" + projectId + "_" + rs.getBigDecimal("bhk").intValue());
          type.put("bhk", rs.getBigDecimal("bhk"));
          type.put("carpetAreaSqft", rs.getBigDecimal("carpet_area"));
          type.put("priceMinInr", rs.getBigDecimal("price_min"));
          type.put("priceMaxInr", rs.getBigDecimal("price_max"));
          type.put("availableUnits", rs.getInt("available"));
          unitTypes.computeIfAbsent(projectId, k -> new ArrayList<>()).add(type);
        });

    Map<String, Map<String, Object>> localities = new LinkedHashMap<>();
    List<Map<String, Object>> projects =
        db.query(
            "SELECT id, name, data->>'location' AS location,"
                + " data->>'possessionDate' AS possession_date, data->>'reraId' AS rera_id"
                + " FROM projects WHERE workspace_id=:ws AND status='ACTIVE' ORDER BY id",
            Map.of("ws", ws),
            (rs, n) -> {
              Map<String, Object> row = new LinkedHashMap<>();
              String location = rs.getString("location");
              String slug = localitySlug(location);
              String name = localityName(location);
              localities.computeIfAbsent(
                  slug,
                  k -> {
                    Map<String, Object> locality = new LinkedHashMap<>();
                    locality.put("id", slug);
                    locality.put("name", name);
                    locality.put("aliases", List.of(name.toLowerCase(Locale.ROOT)));
                    return locality;
                  });
              row.put("id", rs.getString("id"));
              row.put("name", rs.getString("name"));
              // A caller rarely says the full name, so the leading word is offered as an alias.
              String projectName = rs.getString("name");
              String firstWord = projectName.split("\\s+")[0].toLowerCase(Locale.ROOT);
              row.put(
                  "aliases",
                  firstWord.equals(projectName.toLowerCase(Locale.ROOT))
                      ? List.of(projectName.toLowerCase(Locale.ROOT))
                      : List.of(projectName.toLowerCase(Locale.ROOT), firstWord));
              row.put("localityId", slug);
              row.put("possessionDate", rs.getString("possession_date"));
              row.put("reraId", rs.getString("rera_id"));
              row.put("unitTypes", unitTypes.getOrDefault(rs.getLong("id"), List.of()));
              return row;
            });

    // Only sell where there is something to sell.
    List<String> operating =
        projects.stream()
            .filter(p -> !((List<?>) p.get("unitTypes")).isEmpty())
            .map(p -> p.get("localityId").toString())
            .distinct()
            .toList();

    Map<String, Object> out = new LinkedHashMap<>();
    out.put("workspaceId", ws.toString());
    out.put("localities", new ArrayList<>(localities.values()));
    out.put("operatingLocalityIds", operating);
    out.put("projects", projects);
    return out;
  }

  /** Project facts the agent reads out: name, locality, possession, RERA and its unit types. */
  public Map<String, Object> project(Long ws, Long projectId) {
    tenant.require(ws);
    var rows =
        db.query(
            "SELECT id, name, status, data->>'location' AS locality,"
                + " data->>'possessionDate' AS possession_date, data->>'reraId' AS rera_id"
                + " FROM projects WHERE workspace_id=:ws AND id=:id",
            Map.of("ws", ws, "id", projectId),
            (rs, n) -> {
              Map<String, Object> row = new LinkedHashMap<>();
              row.put("id", rs.getString("id"));
              row.put("name", rs.getString("name"));
              row.put("status", rs.getString("status"));
              row.put("localityName", Objects.requireNonNullElse(rs.getString("locality"), ""));
              row.put("possessionDate", rs.getString("possession_date"));
              row.put("reraId", rs.getString("rera_id"));
              return row;
            });
    if (rows.isEmpty()) throw ApiException.missing();
    Map<String, Object> project = new LinkedHashMap<>(rows.getFirst());

    // Unit types are derived from live inventory rather than the unit_types catalogue, so the
    // agent only ever hears about configurations that actually exist and are available.
    project.put(
        "unitTypes",
        db.query(
            "SELECT bhk, min(area) AS carpet_area FROM units"
                + " WHERE workspace_id=:ws AND project_id=:id AND status='AVAILABLE'"
                + " GROUP BY bhk ORDER BY bhk",
            Map.of("ws", ws, "id", projectId),
            (rs, n) ->
                Map.of(
                    "bhk", rs.getBigDecimal("bhk"),
                    "carpetAreaSqft", rs.getBigDecimal("carpet_area"))));
    return project;
  }

  /**
   * Matches for a caller who has described what they want rather than named a project. Results are
   * grouped by project and configuration, because that is the unit of a spoken recommendation.
   */
  public List<Map<String, Object>> searchUnits(
      Long ws, BigDecimal budget, List<BigDecimal> bhk, String locality, int limit) {
    tenant.require(ws);
    if (limit < 1 || limit > 20) throw ApiException.bad("limit must be between 1 and 20");

    StringBuilder where =
        new StringBuilder(
            " WHERE u.workspace_id=:ws AND u.status='AVAILABLE' AND p.status='ACTIVE'");
    Map<String, Object> params = new HashMap<>();
    params.put("ws", ws);
    if (bhk != null && !bhk.isEmpty()) {
      where.append(" AND u.bhk IN (:bhk)");
      params.put("bhk", bhk.stream().map(BigDecimal::intValue).toList());
    }
    if (locality != null && !locality.isBlank()) {
      where.append(" AND p.data->>'location' ILIKE :locality");
      params.put("locality", "%" + locality.trim().replace("%", "\\%").replace("_", "\\_") + "%");
    }
    // A caller's budget is approximate, so allow a little headroom rather than hiding a near miss.
    if (budget != null) {
      where.append(" AND u.price <= :ceiling");
      params.put("ceiling", budget.multiply(new BigDecimal("1.1")));
    }
    params.put("limit", limit);

    // Within budget first, the closest to the budget first; then the near misses above it, cheapest
    // first. Ranking by distance alone put projects over the budget ahead of ones within it.
    String order =
        budget != null
            ? " ORDER BY (min(u.price) <= :budget) DESC,"
                + " CASE WHEN min(u.price) <= :budget THEN -min(u.price) ELSE min(u.price) END ASC,"
                + " available DESC"
            : " ORDER BY min(u.price) ASC, available DESC";
    if (budget != null) params.put("budget", budget);

    return db.query(
        "SELECT p.id AS project_id, p.name AS project_name,"
            + " p.data->>'location' AS locality, p.data->>'possessionDate' AS possession_date,"
            + " u.bhk, min(u.price) AS price_min, max(u.price) AS price_max,"
            + " count(*) AS available"
            + " FROM units u JOIN projects p"
            + "   ON p.workspace_id=u.workspace_id AND p.id=u.project_id"
            + where
            + " GROUP BY p.id, p.name, p.data->>'location', p.data->>'possessionDate', u.bhk"
            + order
            + " LIMIT :limit",
        params,
        (rs, n) -> {
          Map<String, Object> row = new LinkedHashMap<>();
          row.put("projectId", rs.getString("project_id"));
          row.put("projectName", rs.getString("project_name"));
          row.put("localityName", Objects.requireNonNullElse(rs.getString("locality"), ""));
          row.put("bhk", rs.getBigDecimal("bhk"));
          row.put("priceMinInr", rs.getBigDecimal("price_min"));
          row.put("priceMaxInr", rs.getBigDecimal("price_max"));
          // Whether the cheapest available unit is within the budget (not just within the headroom).
          row.put("withinBudget", budget == null || rs.getBigDecimal("price_min").compareTo(budget) <= 0);
          row.put("availableUnits", rs.getInt("available"));
          row.put("possessionDate", rs.getString("possession_date"));
          return row;
        });
  }

  /** How many units are actually free right now, optionally for one configuration. */
  public Map<String, Object> availability(Long ws, Long projectId, BigDecimal bhk) {
    tenant.require(ws);
    Map<String, Object> params = new HashMap<>(Map.of("ws", ws, "id", projectId));
    String filter = "";
    if (bhk != null) {
      filter = " AND bhk=:bhk";
      params.put("bhk", bhk.intValue());
    }
    Long units =
        db.queryForObject(
            "SELECT count(*) FROM units WHERE workspace_id=:ws AND project_id=:id"
                + " AND status='AVAILABLE'"
                + filter,
            params,
            Long.class);
    Map<String, Object> out = new LinkedHashMap<>();
    out.put("projectId", projectId.toString());
    out.put("bhk", bhk);
    out.put("availableUnits", units == null ? 0 : units.intValue());
    out.put("asOf", java.time.Instant.now().toString());
    return out;
  }

  /** Current asking range for one configuration, from available stock only. */
  public Map<String, Object> price(Long ws, Long projectId, BigDecimal bhk) {
    tenant.require(ws);
    var rows =
        db.query(
            "SELECT min(price) AS price_min, max(price) AS price_max, count(*) AS available"
                + " FROM units WHERE workspace_id=:ws AND project_id=:id AND bhk=:bhk"
                + " AND status='AVAILABLE'",
            Map.of("ws", ws, "id", projectId, "bhk", bhk.intValue()),
            (rs, n) -> {
              Map<String, Object> row = new LinkedHashMap<>();
              row.put("priceMinInr", rs.getBigDecimal("price_min"));
              row.put("priceMaxInr", rs.getBigDecimal("price_max"));
              row.put("availableUnits", rs.getInt("available"));
              return row;
            });
    // No available unit means no price to quote; saying nothing is better than quoting a sold one.
    if (rows.isEmpty() || rows.getFirst().get("priceMinInr") == null) throw ApiException.missing();
    Map<String, Object> out = new LinkedHashMap<>(rows.getFirst());
    out.put("projectId", projectId.toString());
    out.put("bhk", bhk);
    out.put("asOf", java.time.Instant.now().toString());
    return out;
  }
}
