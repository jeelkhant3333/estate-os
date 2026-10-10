package com.estraos;

import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;

import com.fasterxml.jackson.databind.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;
import org.springframework.test.web.servlet.*;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.*;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles({"demo", "test"})
class ApiIntegrationTest {
  static PostgreSQLContainer<?> postgres;
  static final String JWT_SECRET = UUID.randomUUID().toString() + UUID.randomUUID();
  static final String PASSWORD = UUID.randomUUID() + "A!";

  @DynamicPropertySource
  static void config(DynamicPropertyRegistry r) {
    String external = System.getenv("TEST_DATABASE_URL");
    if (external == null || external.isBlank()) {
      postgres = new PostgreSQLContainer<>("postgres:17-alpine");
      postgres.start();
      r.add("spring.datasource.url", postgres::getJdbcUrl);
      r.add("spring.datasource.username", postgres::getUsername);
      r.add("spring.datasource.password", postgres::getPassword);
    } else {
      r.add("spring.datasource.url", () -> external);
      r.add("spring.datasource.username", () -> System.getenv("TEST_DATABASE_USERNAME"));
      r.add("spring.datasource.password", () -> System.getenv("TEST_DATABASE_PASSWORD"));
    }
    r.add("app.jwt-secret", () -> JWT_SECRET);
    r.add("app.demo-password", () -> PASSWORD);
    r.add("spring.flyway.enabled", () -> true);
    r.add("app.integrations.mode", () -> "mock");
    r.add("app.integrations.rag.url", () -> "");
    r.add("app.integrations.voice.url", () -> "");
    r.add("app.integrations.notification.url", () -> "");
    r.add("app.notifications.reminders-enabled", () -> false);
    r.add("app.knowledge.sync-enabled", () -> false);
    r.add("app.calls.scheduler-enabled", () -> false);
  }

  @Autowired MockMvc mvc;
  @Autowired ObjectMapper json;
  @Autowired JdbcTemplate db;
  String token, ws, other, agentId, projectId, unitId;

  @AfterAll
  static void stopDatabase() {
    if (postgres != null) postgres.stop();
  }

  @BeforeEach
  void login() throws Exception {
    JsonNode auth =
        send(
            "POST",
            "/auth/login",
            Map.of("email", "admin@estraos.demo", "password", PASSWORD),
            null,
            null,
            200);
    token = auth.get("token").asText();
    ws = auth.get("workspaces").get(0).get("id").asText();
    other = auth.get("workspaces").get(1).get("id").asText();
    for (JsonNode member : send("GET", "/members", null, token, ws, 200))
      if (member.get("role").asText().equals("REAL_ESTATE_AGENT"))
        agentId = member.get("id").asText();
    projectId =
        send("GET", "/projects?size=100", null, token, ws, 200)
            .get("items")
            .get(0)
            .get("id")
            .asText();
    for (JsonNode unit :
        send("GET", "/units?projectId=" + projectId, null, token, ws, 200).get("items"))
      if (unit.get("status").asText().equals("AVAILABLE")) {
        unitId = unit.get("id").asText();
        break;
      }
  }

  JsonNode send(
      String method, String path, Object body, String bearer, String workspace, int expected)
      throws Exception {
    var request =
        switch (method) {
          case "POST" -> post("/api/v1" + path);
          case "PUT" -> put("/api/v1" + path);
          case "DELETE" -> delete("/api/v1" + path);
          default -> get("/api/v1" + path);
        };
    request.contentType(MediaType.APPLICATION_JSON);
    if (body != null) request.content(json.writeValueAsBytes(body));
    if (bearer != null) request.header("Authorization", "Bearer " + bearer);
    if (workspace != null) request.header("X-Workspace-Id", workspace);
    var result = mvc.perform(request).andReturn();
    assertEquals(
        expected,
        result.getResponse().getStatus(),
        method + " " + path + " " + result.getResponse().getContentAsString());
    return result.getResponse().getContentAsString().isBlank()
        ? json.nullNode()
        : json.readTree(result.getResponse().getContentAsString());
  }

  Map<String, Object> leadBody() {
    String suffix = Long.toString(System.nanoTime());
    return new LinkedHashMap<>(
        Map.of(
            "name",
            "API test customer",
            "phone",
            "+91" + suffix.substring(suffix.length() - 10),
            "email",
            suffix + "@example.invalid",
            "language",
            "en",
            "intent",
            "BUY",
            "status",
            "NEW",
            "assignedAgentId",
            agentId));
  }

  Map<String, Object> visitBody(String lead, Instant start) {
    return new LinkedHashMap<>(
        Map.of(
            "leadId",
            lead,
            "projectId",
            projectId,
            "unitId",
            unitId,
            "agentId",
            agentId,
            "scheduledAt",
            start.toString(),
            "durationMinutes",
            60,
            "status",
            "CONFIRMED"));
  }

  @Test
  void identitySchemaAndAuthentication() throws Exception {
    assertTrue(ws.matches("[1-9][0-9]*"));
    assertTrue(
        db.queryForList(
                "SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND"
                    + " column_name='id' AND (data_type<>'bigint' OR is_identity<>'YES')",
                String.class)
            .isEmpty());
    send("GET", "/auth/me", null, token, null, 200);
    send(
        "POST",
        "/auth/login",
        Map.of("email", "admin@estraos.demo", "password", "invalid"),
        null,
        null,
        401);
    send("POST", "/auth/logout", Map.of(), token, null, 204);
    send("GET", "/auth/me", null, token, null, 401);
    send("GET", "/projects", null, null, ws, 401);
  }

  @Test
  void tenantAndRoleIsolation() throws Exception {
    var login =
        send(
            "POST",
            "/auth/login",
            Map.of("email", "agent@estraos.demo", "password", PASSWORD),
            null,
            null,
            200);
    String agentToken = login.get("token").asText();
    send("GET", "/projects", null, agentToken, other, 403);
    send("GET", "/audit-logs", null, agentToken, ws, 403);
    send(
        "POST",
        "/projects",
        Map.of("name", "Denied", "location", "Ahmedabad"),
        agentToken,
        ws,
        403);
    String foreignProject =
        send("GET", "/projects", null, token, other, 200).get("items").get(0).get("id").asText();
    send("GET", "/projects/" + foreignProject, null, token, ws, 404);
    send(
        "PUT",
        "/projects/" + foreignProject,
        Map.of("name", "Denied cross tenant", "location", "Pune"),
        token,
        ws,
        404);
    var foreignUnit =
        Map.of(
            "projectId",
            foreignProject,
            "unitNumber",
            "X-1",
            "bhk",
            2,
            "area",
            1000,
            "price",
            8000000,
            "propertyType",
            "APARTMENT");
    send("POST", "/units", foreignUnit, token, ws, 404);
    send("GET", "/projects?agentId=1", null, token, ws, 400);
    send("GET", "/projects", null, token, "-1", 400);
  }

  @Test
  void leadValidationDuplicatesAndRecommendations() throws Exception {
    Map<String, Object> body = leadBody();
    var lead = send("POST", "/leads", body, token, ws, 201);
    String id = lead.get("id").asText();
    send("POST", "/leads", body, token, ws, 409);
    body.put("budgetMin", 100);
    body.put("budgetMax", 10);
    send("PUT", "/leads/" + id, body, token, ws, 400);
    send(
        "POST",
        "/leads/" + id + "/notes",
        Map.of("text", "Customer confirmed requirements"),
        token,
        ws,
        201);
    var result = send("POST", "/recommendations/search", Map.of("budgetMax", 1), token, ws, 200);
    assertEquals(0, result.get("items").size());
    var available =
        send("POST", "/recommendations/search", Map.of("projectId", projectId), token, ws, 200);
    assertTrue(available.get("items").size() > 0);
    for (var item : available.get("items"))
      assertEquals("AVAILABLE", item.get("unit").get("status").asText());
    send(
        "POST",
        "/leads/" + id + "/recommendations",
        Map.of("unitIds", List.of(unitId)),
        token,
        ws,
        200);
    assertEquals(
        1, send("GET", "/leads/" + id, null, token, ws, 200).get("recommendations").size());
  }

  @Test
  void documentDraftPublishVersionAndWorkspace() throws Exception {
    var doc =
        send(
            "POST",
            "/documents",
            Map.of(
                "projectId",
                projectId,
                "title",
                "Test brochure",
                "fileName",
                "test.pdf",
                "storageReference",
                "s3://test/brochure.pdf",
                "language",
                "gu",
                "content",
                "ગુજરાતી માહિતી"),
            token,
            ws,
            201);
    String id = doc.get("id").asText();
    send("POST", "/documents/" + id + "/reindex", Map.of(), token, ws, 400);
    assertEquals(
        "PUBLISHED",
        send("POST", "/documents/" + id + "/publish", Map.of(), token, ws, 200)
            .get("status")
            .asText());
    send(
        "PUT",
        "/documents/" + id + "/content",
        Map.of("content", "updated", "language", "xx"),
        token,
        ws,
        400);
    assertEquals(
        "PUBLISHED", send("GET", "/documents/" + id, null, token, ws, 200).get("status").asText());
    assertEquals(
        "DRAFT",
        send(
                "PUT",
                "/documents/" + id + "/content",
                Map.of("content", "Updated brochure", "language", "en"),
                token,
                ws,
                200)
            .get("status")
            .asText());
    assertTrue(send("GET", "/documents/" + id + "/versions", null, token, ws, 200).size() >= 3);
    send("GET", "/documents/" + id, null, token, other, 404);
  }

  @Test
  void overlapCancellationHandoverAndNotificationOwnership() throws Exception {
    String lead = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    String secondLead = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    Instant start = Instant.now().plusSeconds(20 * 86400);
    Map<String, Object> body = visitBody(lead, start);
    String visit = send("POST", "/site-visits", body, token, ws, 201).get("id").asText();
    body.put("confirmationStatus", "CONFIRMED");
    assertEquals(
        "CONFIRMED",
        send("PUT", "/site-visits/" + visit, body, token, ws, 200)
            .get("confirmationStatus")
            .asText());
    body.remove("confirmationStatus");
    assertEquals(
        "CONFIRMED",
        send("PUT", "/site-visits/" + visit, body, token, ws, 200)
            .get("confirmationStatus")
            .asText());
    send("POST", "/site-visits", visitBody(secondLead, start.plusSeconds(1800)), token, ws, 409);
    body.put("status", "CANCELLED");
    send("PUT", "/site-visits/" + visit, body, token, ws, 200);
    String replacement =
        send("POST", "/site-visits", visitBody(lead, start), token, ws, 201).get("id").asText();
    Map<String, Object> handover =
        new LinkedHashMap<>(
            Map.of(
                "leadId",
                secondLead,
                "agentId",
                agentId,
                "projectId",
                projectId,
                "unitId",
                unitId,
                "appointmentId",
                replacement,
                "status",
                "ASSIGNED"));
    send("POST", "/handovers", handover, token, ws, 400);
    handover.put("leadId", lead);
    String handoverId = send("POST", "/handovers", handover, token, ws, 201).get("id").asText();
    for (String state : List.of("ACCEPTED", "COMPLETED")) {
      handover.put("status", state);
      assertEquals(
          state,
          send("PUT", "/handovers/" + handoverId, handover, token, ws, 200).get("status").asText());
    }
    var update = visitBody(secondLead, start);
    send("PUT", "/site-visits/" + replacement, update, token, ws, 409);
    Long notification =
        db.queryForObject(
            "SELECT id FROM notifications WHERE workspace_id=? AND user_id=? ORDER BY id DESC LIMIT"
                + " 1",
            Long.class,
            Long.valueOf(ws),
            Long.valueOf(agentId));
    send("POST", "/notifications/" + notification + "/read", Map.of(), token, ws, 403);
  }

  @Test
  void concurrentBookingsSerialize() throws Exception {
    String lead = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    Map<String, Object> body = visitBody(lead, Instant.now().plusSeconds(30 * 86400));
    String encoded = json.writeValueAsString(body);
    Callable<Integer> submit =
        () ->
            mvc.perform(
                    post("/api/v1/site-visits")
                        .header("Authorization", "Bearer " + token)
                        .header("X-Workspace-Id", ws)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(encoded))
                .andReturn()
                .getResponse()
                .getStatus();
    try (var executor = Executors.newFixedThreadPool(2)) {
      var results = executor.invokeAll(List.of(submit, submit));
      List<Integer> statuses = new ArrayList<>();
      for (var result : results) statuses.add(result.get());
      Collections.sort(statuses);
      assertEquals(List.of(201, 409), statuses);
    }
  }

  @Test
  void inventoryDuplicatesHistoryAndCurrentAvailability() throws Exception {
    String project =
        send(
                "POST",
                "/projects",
                Map.of(
                    "name", "Inventory verification", "location", "Ahmedabad", "status", "ACTIVE"),
                token,
                ws,
                201)
            .get("id")
            .asText();
    Map<String, Object> unit =
        new LinkedHashMap<>(
            Map.of(
                "projectId",
                project,
                "unitNumber",
                "TEST-1",
                "bhk",
                2,
                "area",
                1200,
                "price",
                7000000,
                "propertyType",
                "APARTMENT",
                "status",
                "AVAILABLE"));
    String resource = send("POST", "/units", unit, token, ws, 201).get("id").asText();
    send("POST", "/units", unit, token, ws, 409);
    unit.put("unitNumber", "test-1");
    send("POST", "/units", unit, token, ws, 409);
    assertEquals(
        1,
        send(
                "POST",
                "/recommendations/search",
                Map.of("projectId", project, "budgetMax", 8000000, "bhk", 2, "areaMin", 1000),
                token,
                ws,
                200)
            .get("items")
            .size());
    unit.put("status", "SOLD");
    send("PUT", "/units/" + resource, unit, token, ws, 200);
    assertEquals(2, send("GET", "/units/" + resource + "/history", null, token, ws, 200).size());
    assertEquals(
        0,
        send("POST", "/recommendations/search", Map.of("projectId", project), token, ws, 200)
            .get("items")
            .size());
    String lead = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    Map<String, Object> visit = visitBody(lead, Instant.now().plusSeconds(45 * 86400));
    visit.put("projectId", project);
    visit.put("unitId", resource);
    send("POST", "/site-visits", visit, token, ws, 409);
    send(
        "POST",
        "/leads/" + lead + "/recommendations",
        Map.of("unitIds", List.of(resource)),
        token,
        ws,
        409);
  }

  @Test
  void persistedRemindersRescheduleAndCancel() throws Exception {
    Map<String, Object> lead = leadBody();
    lead.put("followUpAt", Instant.now().plusSeconds(4 * 86400).toString());
    String resource = send("POST", "/leads", lead, token, ws, 201).get("id").asText();
    assertEquals(1, reminderCount("CALLBACK_REMINDER", resource, "SCHEDULED"));
    lead.put("followUpAt", Instant.now().plusSeconds(5 * 86400).toString());
    send("PUT", "/leads/" + resource, lead, token, ws, 200);
    assertEquals(1, reminderCount("CALLBACK_REMINDER", resource, "SCHEDULED"));
    assertEquals(1, reminderCount("CALLBACK_REMINDER", resource, "CANCELLED"));
    lead.put("followUpAt", null);
    send("PUT", "/leads/" + resource, lead, token, ws, 200);
    assertEquals(0, reminderCount("CALLBACK_REMINDER", resource, "SCHEDULED"));
    Map<String, Object> visit = visitBody(resource, Instant.now().plusSeconds(50 * 86400));
    String appointment = send("POST", "/site-visits", visit, token, ws, 201).get("id").asText();
    assertEquals(1, reminderCount("SITE_VISIT_REMINDER", appointment, "SCHEDULED"));
    visit.put("scheduledAt", Instant.now().plusSeconds(51 * 86400).toString());
    visit.put("status", "RESCHEDULED");
    send("PUT", "/site-visits/" + appointment, visit, token, ws, 200);
    assertEquals(1, reminderCount("SITE_VISIT_REMINDER", appointment, "SCHEDULED"));
    assertEquals(1, reminderCount("SITE_VISIT_REMINDER", appointment, "CANCELLED"));
    visit.put("status", "CANCELLED");
    send("PUT", "/site-visits/" + appointment, visit, token, ws, 200);
    assertEquals(0, reminderCount("SITE_VISIT_REMINDER", appointment, "SCHEDULED"));
  }

  @Test
  void browserPreflightIsAllowedFromTheConfiguredOrigin() throws Exception {
    // The dev server proxies /api on the same origin, so CORS only ever runs in a real
    // deployment. Assert it here instead of discovering it after a deploy.
    mvc.perform(
            options("/api/v1/auth/login")
                .header("Origin", "http://localhost:5173")
                .header("Access-Control-Request-Method", "POST")
                .header("Access-Control-Request-Headers", "content-type,authorization"))
        .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isOk())
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.header()
                .string("Access-Control-Allow-Origin", "http://localhost:5173"));

    mvc.perform(
            options("/api/v1/auth/login")
                .header("Origin", "https://attacker.example")
                .header("Access-Control-Request-Method", "POST"))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isForbidden());
  }

  // ---- voice agent integration (docs/integration.md "Voice agent access to this API")

  private Map<String, Object> callRecord(String leadId, String sessionId) {
    return new LinkedHashMap<>(
        Map.ofEntries(
            Map.entry("callId", sessionId),
            Map.entry("voiceSessionId", sessionId),
            Map.entry("leadId", Long.valueOf(leadId)),
            Map.entry("customerPhone", "+919876500011"),
            Map.entry("direction", "inbound"),
            Map.entry("durationSeconds", 42.5),
            Map.entry("language", "hi"),
            Map.entry("customerName", "Asha Patil"),
            Map.entry("budget", 7500000),
            Map.entry("bhk", List.of(2)),
            Map.entry("location", "Baner"),
            Map.entry("intent", "high"),
            Map.entry("leadScore", 65),
            Map.entry("summary", "Wants a 2 BHK in Baner within 7.5 lakh budget."),
            Map.entry("transcript", List.of(Map.of("speaker", "caller", "text", "namaste")))));
  }

  @Test
  void voiceAgentResolvesACallerToALeadExactlyOnce() throws Exception {
    Map<String, Object> lookup = Map.of("phone", "9876500011", "source", "VOICE_AGENT", "language", "hi");

    JsonNode created = send("POST", "/leads/find-or-create", lookup, token, ws, 200);
    assertTrue(created.get("created").asBoolean());
    assertEquals("+919876500011", created.get("phone").asText());
    assertEquals("NEW", created.get("status").asText());

    // A second call from the same number must reuse the lead, not create a duplicate.
    JsonNode found = send("POST", "/leads/find-or-create", lookup, token, ws, 200);
    assertFalse(found.get("created").asBoolean());
    assertEquals(created.get("id").asText(), found.get("id").asText());

    // The same number in another workspace is a different person.
    JsonNode elsewhere = send("POST", "/leads/find-or-create", lookup, token, other, 200);
    assertTrue(elsewhere.get("created").asBoolean());
    assertNotEquals(created.get("id").asText(), elsewhere.get("id").asText());
  }

  @Test
  void callRecordIsStoredOnceAndMovesTheLeadForward() throws Exception {
    String leadId =
        send("POST", "/leads/find-or-create", Map.of("phone", "9876500011"), token, ws, 200)
            .get("id")
            .asText();
    Map<String, Object> record = callRecord(leadId, "call_" + System.nanoTime());

    JsonNode stored = send("POST", "/calls/ingest", record, token, ws, 200);
    assertEquals("COMPLETED", stored.get("status").asText());
    assertEquals(leadId, stored.get("leadId").asText());
    assertEquals("Baner", stored.get("location").asText());

    // The agent's outbox may resend after a timeout; that must not create a second session.
    JsonNode again = send("POST", "/calls/ingest", record, token, ws, 200);
    assertEquals(stored.get("id").asText(), again.get("id").asText());

    JsonNode lead = send("GET", "/leads/" + leadId, null, token, ws, 200);
    assertEquals("CONTACTED", lead.get("status").asText());
    assertEquals("Asha Patil", lead.get("customerName").asText());
    assertEquals(1, lead.get("calls").size());
    assertEquals(
        1,
        db.queryForObject(
            "SELECT count(*) FROM voice_transcripts WHERE workspace_id=? AND voice_session_id=?",
            Long.class,
            Long.valueOf(ws),
            Long.valueOf(stored.get("id").asText()))
            .intValue());
  }

  @Test
  void doNotCallOutcomeClosesTheLead() throws Exception {
    String leadId =
        send("POST", "/leads/find-or-create", Map.of("phone", "9876500022"), token, ws, 200)
            .get("id")
            .asText();
    Map<String, Object> record = callRecord(leadId, "call_" + System.nanoTime());
    record.put("customerPhone", "+919876500022");
    record.put("doNotCall", true);
    record.put("doNotCallBasis", "explicit");

    send("POST", "/calls/ingest", record, token, ws, 200);
    JsonNode lead = send("GET", "/leads/" + leadId, null, token, ws, 200);
    assertEquals("NOT_INTERESTED", lead.get("status").asText());
    assertTrue(lead.get("doNotCall").asBoolean());
  }

  @Test
  void aCallCannotBeReassignedToAnotherLead() throws Exception {
    String first =
        send("POST", "/leads/find-or-create", Map.of("phone", "9876500033"), token, ws, 200)
            .get("id")
            .asText();
    String second =
        send("POST", "/leads/find-or-create", Map.of("phone", "9876500044"), token, ws, 200)
            .get("id")
            .asText();
    String sessionId = "call_" + System.nanoTime();
    send("POST", "/calls/ingest", callRecord(first, sessionId), token, ws, 200);

    Map<String, Object> stolen = callRecord(second, sessionId);
    send("POST", "/calls/ingest", stolen, token, ws, 409);
  }

  @Test
  void voiceEndpointsRejectUnauthenticatedAndCrossWorkspaceCallers() throws Exception {
    Map<String, Object> lookup = Map.of("phone", "9876500055");
    send("POST", "/leads/find-or-create", lookup, null, ws, 401);

    String leadId = send("POST", "/leads/find-or-create", lookup, token, ws, 200).get("id").asText();
    // The lead belongs to `ws`; ingesting it against `other` must not reach across the tenant line.
    send("POST", "/calls/ingest", callRecord(leadId, "call_x"), token, other, 404);
  }

  // ---- voice catalogue: inventory the agent quotes on a live call

  @Test
  void voiceProjectLookupReturnsOnlyAvailableConfigurations() throws Exception {
    JsonNode project = send("GET", "/voice/projects/" + projectId, null, token, ws, 200);
    assertEquals(projectId, project.get("id").asText());
    assertTrue(project.has("localityName"));
    assertTrue(project.get("unitTypes").isArray());
    // Every configuration offered must correspond to stock that is actually available.
    for (JsonNode type : project.get("unitTypes")) {
      long available =
          db.queryForObject(
              "SELECT count(*) FROM units WHERE workspace_id=? AND project_id=? AND bhk=?"
                  + " AND status='AVAILABLE'",
              Long.class,
              Long.valueOf(ws),
              Long.valueOf(projectId),
              type.get("bhk").asInt());
      assertTrue(available > 0, "offered a BHK with no available unit");
    }
  }

  @Test
  void voiceUnitSearchRespectsBudgetAndNeverReturnsSoldStock() throws Exception {
    JsonNode all = send("POST", "/voice/units/search", Map.of("limit", 20), token, ws, 200);
    assertTrue(all.get("matches").size() > 0, "demo workspace should have available units");

    for (JsonNode match : all.get("matches")) {
      assertTrue(match.get("availableUnits").asInt() > 0);
      long sold =
          db.queryForObject(
              "SELECT count(*) FROM units WHERE workspace_id=? AND project_id=? AND bhk=?"
                  + " AND status<>'AVAILABLE' AND price=?",
              Long.class,
              Long.valueOf(ws),
              match.get("projectId").asLong(),
              match.get("bhk").asInt(),
              match.get("priceMinInr").decimalValue());
      assertEquals(0, sold, "quoted a price that belongs to non-available stock");
    }

    // A tight budget must not return everything; headroom is 10%.
    JsonNode cheap =
        send("POST", "/voice/units/search", Map.of("budgetInr", 1, "limit", 20), token, ws, 200);
    assertEquals(0, cheap.get("matches").size());
  }

  @Test
  void voiceUnitSearchPutsOptionsWithinBudgetBeforeNearMisses() throws Exception {
    JsonNode all = send("POST", "/voice/units/search", Map.of("limit", 20), token, ws, 200);
    // A budget just above the cheapest option: everything up to 10% over it may still be offered.
    java.math.BigDecimal budget = all.get("matches").get(0).get("priceMinInr").decimalValue()
        .add(new java.math.BigDecimal("1"));
    JsonNode matches =
        send("POST", "/voice/units/search", Map.of("budgetInr", budget, "limit", 20), token, ws, 200)
            .get("matches");
    assertTrue(matches.size() > 0);
    boolean seenOver = false;
    for (JsonNode m : matches) {
      boolean within = m.get("priceMinInr").decimalValue().compareTo(budget) <= 0;
      assertEquals(within, m.get("withinBudget").asBoolean(), "withinBudget flag is wrong");
      if (!within) seenOver = true;
      else assertFalse(seenOver, "an option over budget was ranked above one within it");
    }
  }

  @Test
  void voiceAvailabilityAndPriceComeFromLiveStock() throws Exception {
    JsonNode match =
        send("POST", "/voice/units/search", Map.of("limit", 1), token, ws, 200).get("matches").get(0);
    String pid = match.get("projectId").asText();
    int bhk = match.get("bhk").asInt();

    JsonNode availability =
        send("GET", "/voice/projects/" + pid + "/availability?bhk=" + bhk, null, token, ws, 200);
    assertEquals(match.get("availableUnits").asInt(), availability.get("availableUnits").asInt());

    JsonNode price = send("GET", "/voice/projects/" + pid + "/price?bhk=" + bhk, null, token, ws, 200);
    assertEquals(0, price.get("priceMinInr").decimalValue()
        .compareTo(match.get("priceMinInr").decimalValue()));

    // A configuration that does not exist has no price rather than a misleading one.
    send("GET", "/voice/projects/" + pid + "/price?bhk=19", null, token, ws, 404);
  }

  @Test
  void voiceCatalogueIsScopedToTheWorkspace() throws Exception {
    send("GET", "/voice/projects/" + projectId, null, token, other, 404);
    send("GET", "/voice/projects/" + projectId, null, null, ws, 401);
  }

  // ---- PDF upload

  private org.springframework.mock.web.MockMultipartFile pdf(String name, String body) {
    return new org.springframework.mock.web.MockMultipartFile(
        "files", name, "application/pdf", ("%PDF-1.4\n" + body).getBytes());
  }

  @Test
  void bulkUploadCreatesADocumentPerPdfAndSkipsBadFiles() throws Exception {
    var good = pdf("brochure-a.pdf", "one");
    var alsoGood = pdf("brochure-b.pdf", "two");
    var notPdf =
        new org.springframework.mock.web.MockMultipartFile(
            "files", "notes.pdf", "application/pdf", "this is not a pdf".getBytes());

    var result =
        mvc.perform(
                multipart("/api/v1/projects/" + projectId + "/documents/upload")
                    .file(good)
                    .file(alsoGood)
                    .file(notPdf)
                    .header("Authorization", "Bearer " + token)
                    .header("X-Workspace-Id", ws))
            .andExpect(
                org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isOk())
            .andReturn();
    JsonNode body = json.readTree(result.getResponse().getContentAsString());

    // One bad file in a dropped folder must not discard the good ones.
    assertEquals(2, body.get("uploadedCount").asInt());
    assertEquals(1, body.get("rejectedCount").asInt());
    assertEquals("notes.pdf", body.get("rejected").get(0).get("filename").asText());

    String documentId = body.get("uploaded").get(0).get("documentId").asText();
    JsonNode doc = send("GET", "/documents/" + documentId, null, token, ws, 200);
    assertEquals("DRAFT", doc.get("status").asText());

    JsonNode info = send("GET", "/documents/" + documentId + "/file/info", null, token, ws, 200);
    assertEquals("brochure-a.pdf", info.get("filename").asText());
    assertTrue(info.get("sizeBytes").asLong() > 0);
  }

  @Test
  void uploadedPdfDownloadsAsAnAttachment() throws Exception {
    var result =
        mvc.perform(
                multipart("/api/v1/projects/" + projectId + "/documents/upload")
                    .file(pdf("plan.pdf", "floor plan"))
                    .header("Authorization", "Bearer " + token)
                    .header("X-Workspace-Id", ws))
            .andReturn();
    String documentId =
        json.readTree(result.getResponse().getContentAsString())
            .get("uploaded").get(0).get("documentId").asText();

    mvc.perform(
            get("/api/v1/documents/" + documentId + "/file")
                .header("Authorization", "Bearer " + token)
                .header("X-Workspace-Id", ws))
        .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isOk())
        // Never inline: a PDF rendered same-origin can script against the app.
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.header()
                .string("Content-Disposition", org.hamcrest.Matchers.containsString("attachment")))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.content()
                .contentType("application/pdf"));
  }

  @Test
  void anUploadedFileIsIndexedAutomaticallyAndTheDocumentFollowsTheKnowledgeService()
      throws Exception {
    var result =
        mvc.perform(
                multipart("/api/v1/projects/" + projectId + "/documents/upload")
                    .file(
                        new org.springframework.mock.web.MockMultipartFile(
                            "files", "price-sheet.csv", "text/csv",
                            "Charge,Amount\nFloor rise,₹40 per sq ft\n".getBytes()))
                    .header("Authorization", "Bearer " + token)
                    .header("X-Workspace-Id", ws))
            .andReturn();
    String documentId =
        json.readTree(result.getResponse().getContentAsString())
            .get("uploaded").get(0).get("documentId").asText();
    JsonNode uploaded = send("GET", "/documents/" + documentId, null, token, ws, 200);
    assertEquals("DRAFT", uploaded.get("status").asText());
    assertEquals("UPLOADED", uploaded.get("processingStatus").asText());
    assertTrue(uploaded.hasNonNull("ragSourceId"));

    // The demo adapter reports the upload as published on the next status check, as the
    // knowledge service does once embedding finishes; the document follows it.
    JsonNode status = send("GET", "/documents/" + documentId + "/processing-status", null, token, ws, 200);
    assertEquals("PUBLISHED", status.get("status").asText());
    assertEquals("PUBLISHED", status.get("documentStatus").asText());
    assertEquals(
        "PUBLISHED", send("GET", "/documents/" + documentId, null, token, ws, 200).get("status").asText());
  }

  @Test
  void uploadRejectsNonPdfContentWhateverTheDeclaredType() throws Exception {
    var disguised =
        new org.springframework.mock.web.MockMultipartFile(
            "files", "payload.pdf", "application/pdf", "MZ\u0090executable".getBytes());
    mvc.perform(
            multipart("/api/v1/projects/" + projectId + "/documents/upload")
                .file(disguised)
                .header("Authorization", "Bearer " + token)
                .header("X-Workspace-Id", ws))
        .andExpect(
            org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isBadRequest());
  }

  @Test
  void listDateRangeFiltersBeforePaginationAndUsesVisitSchedule() throws Exception {
    var first = send("POST", "/leads", leadBody(), token, ws, 201);
    var second = send("POST", "/leads", leadBody(), token, ws, 201);
    db.update("UPDATE leads SET created_at='2020-01-05T08:00:00Z' WHERE id=?", first.get("id").asLong());
    db.update("UPDATE leads SET created_at='2020-01-06T08:00:00Z' WHERE id=?", second.get("id").asLong());

    var filtered = send("GET", "/leads?dateFrom=2020-01-05T00:00:00Z&dateTo=2020-01-06T00:00:00Z&size=1", null, token, ws, 200);
    assertEquals(1, filtered.get("total").asInt());
    assertEquals(first.get("id").asText(), filtered.get("items").get(0).get("id").asText());

    Long visitId = db.queryForObject("INSERT INTO appointments(workspace_id,project_id,lead_id,agent_id,scheduled_at,duration_minutes,status,created_at) VALUES (?,?,?,?,'2020-01-05T12:00:00Z',60,'CONFIRMED','2020-01-04T12:00:00Z') RETURNING id", Long.class,
        Long.valueOf(ws), Long.valueOf(projectId), first.get("id").asLong(), Long.valueOf(agentId));
    var visits = send("GET", "/site-visits?dateFrom=2020-01-05T00:00:00Z&dateTo=2020-01-06T00:00:00Z", null, token, ws, 200);
    assertTrue(visits.get("items").toString().contains(visitId.toString()));
    send("GET", "/leads?dateFrom=not-a-date", null, token, ws, 400);
  }

  @Test
  void managedFileListFiltersByUploadDateBeforeCounting() throws Exception {
    Long userId = db.queryForObject("SELECT id FROM users WHERE email='admin@estraos.demo'", Long.class);
    db.update("INSERT INTO managed_files(workspace_id,original_file_name,file_extension,file_size_bytes,status,created_by,created_at) VALUES (?,?,?,?,?,?,?::timestamptz)",
        Long.valueOf(ws), "old-date-filter.txt", "txt", 1, "STORED", userId, "2020-02-01T10:00:00Z");
    db.update("INSERT INTO managed_files(workspace_id,original_file_name,file_extension,file_size_bytes,status,created_by,created_at) VALUES (?,?,?,?,?,?,?::timestamptz)",
        Long.valueOf(ws), "new-date-filter.txt", "txt", 1, "STORED", userId, "2020-02-02T10:00:00Z");
    var filtered = send("GET", "/files?dateFrom=2020-02-01T00:00:00Z&dateTo=2020-02-02T00:00:00Z&size=1", null, token, ws, 200);
    assertEquals(1, filtered.get("total").asInt());
    assertEquals("old-date-filter.txt", filtered.get("items").get(0).get("originalFileName").asText());
  }

  @Test
  void removingLeadHidesItCancelsScheduledContactAndFreesItsPhone() throws Exception {
    Map<String, Object> body = leadBody();
    String leadId = send("POST", "/leads", body, token, ws, 201).get("id").asText();
    send("POST", "/leads/" + leadId + "/callbacks",
        Map.of("dueAt", Instant.now().plusSeconds(86400).toString(), "reason", "Follow up"),
        token, ws, 201);
    long before = send("GET", "/dashboard", null, token, ws, 200)
        .get("metrics").get("totalLeads").asLong();

    send("DELETE", "/leads/" + leadId, null, token, other, 404);
    send("DELETE", "/leads/" + leadId, null, token, ws, 204);
    send("GET", "/leads/" + leadId, null, token, ws, 404);
    assertFalse(send("GET", "/leads?size=100", null, token, ws, 200).get("items").toString()
        .contains("\"id\":\"" + leadId + "\""));
    assertEquals(before - 1, send("GET", "/dashboard", null, token, ws, 200)
        .get("metrics").get("totalLeads").asLong());
    assertEquals("CANCELLED", db.queryForObject(
        "SELECT status FROM callbacks WHERE workspace_id=? AND lead_id=? ORDER BY id DESC LIMIT 1",
        String.class, Long.valueOf(ws), Long.valueOf(leadId)));
    assertEquals(0L, db.queryForObject(
        "SELECT count(*) FROM scheduled_calls WHERE workspace_id=? AND lead_id=? AND status='SCHEDULED'",
        Long.class, Long.valueOf(ws), Long.valueOf(leadId)));
    assertNotEquals(leadId, send("POST", "/leads", body, token, ws, 201).get("id").asText());
  }

  @Test
  void activeVisitBlocksLeadRemovalUntilItIsCancelled() throws Exception {
    String leadId = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    Map<String, Object> visit = new LinkedHashMap<>(Map.of(
        "leadId", leadId, "projectId", projectId, "agentId", agentId,
        "scheduledAt", Instant.now().plusSeconds(400L * 86400).toString(),
        "durationMinutes", 60, "status", "CONFIRMED"));
    String visitId = send("POST", "/site-visits", visit, token, ws, 201).get("id").asText();

    send("DELETE", "/leads/" + leadId, null, token, ws, 409);
    visit.put("status", "CANCELLED");
    send("PUT", "/site-visits/" + visitId, visit, token, ws, 200);
    send("DELETE", "/leads/" + leadId, null, token, ws, 204);
  }

  @Test
  void completedConversationCanBeRemovedButQueuedOneCannot() throws Exception {
    String leadId = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    Map<String, Object> record = callRecord(leadId, "delete-test-" + System.nanoTime());
    String completedId = send("POST", "/calls/ingest",
        record, token, ws, 200).get("id").asText();
    long queuedId = db.queryForObject(
        "INSERT INTO voice_sessions(workspace_id,lead_id,status,data) VALUES (?,?,?, '{}'::jsonb) RETURNING id",
        Long.class, Long.valueOf(ws), Long.valueOf(leadId), "QUEUED");

    send("DELETE", "/calls/" + queuedId, null, token, ws, 409);
    send("DELETE", "/calls/" + completedId, null, token, other, 404);
    send("DELETE", "/calls/" + completedId, null, token, ws, 204);
    send("GET", "/calls/" + completedId, null, token, ws, 404);
    assertEquals(completedId,
        send("POST", "/calls/ingest", record, token, ws, 200).get("id").asText());
    send("GET", "/calls/" + completedId, null, token, ws, 404);
    assertFalse(send("GET", "/leads/" + leadId, null, token, ws, 200).get("calls").toString()
        .contains("\"id\":\"" + completedId + "\""));
  }

  @Test
  void agentsCannotRemoveLeadsOrConversations() throws Exception {
    String leadId = send("POST", "/leads", leadBody(), token, ws, 201).get("id").asText();
    String callId = send("POST", "/calls/ingest",
        callRecord(leadId, "agent-delete-test-" + System.nanoTime()), token, ws, 200)
        .get("id").asText();
    String agentToken = send("POST", "/auth/login",
        Map.of("email", "agent@estraos.demo", "password", PASSWORD), null, null, 200)
        .get("token").asText();
    send("DELETE", "/leads/" + leadId, null, agentToken, ws, 403);
    send("DELETE", "/calls/" + callId, null, agentToken, ws, 403);
    send("GET", "/leads/" + leadId, null, token, ws, 200);
    send("GET", "/calls/" + callId, null, token, ws, 200);
  }

  private long reminderCount(String type, String resource, String status) {
    return db.queryForObject(
        "SELECT count(*) FROM notifications WHERE workspace_id=? AND status=? AND"
            + " data->>'type'=? AND data->>'resourceId'=?",
        Long.class,
        Long.valueOf(ws),
        status,
        type,
        resource);
  }
}
