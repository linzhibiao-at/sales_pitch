/**
 * 横切关注点（Cross-Cutting Concerns）层。
 * <p>
 * 包含 {@code ApiClient}（统一 HTTP 抽象）、日志、指标、安全等基础设施实现。
 * 这些组件必须通过 Spring 注入使用，禁止 {@code new} 实例化。
 */
package com.example.app.infrastructure;
