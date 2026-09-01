# AGENTS.md

## 项目简介
任务管理 Web 应用，基于 Spring Boot 2.7 + Java 1.8 + MySQL 5.7 + MyBatis-Plus 3.5.x。

## 技术栈基线（不允许擅自升级）
- JDK: 1.8（不可使用 Java 9+ 语法）
- Spring Boot: 2.7.x（不可升 3.x）
- Maven: 3.6.3（由 enforcer 强制）

## 快速导航
| 你想做什么 | 去哪里看 |
|-----------|---------|
| 了解系统架构 | docs/architecture/overview.md |
| 了解编码规范 | docs/conventions/README.md |
| 了解当前任务 | docs/plans/current-sprint.md |

## 硬性规则
1. 依赖方向：domain → config → mapper → service → controller
2. 禁止 System.out / printStackTrace，统一 SLF4J 结构化日志
3. 单文件 ≤ 300 行；单方法 ≤ 50 行
4. 新功能必须有 JUnit 5 测试，覆盖率 ≥ 80%
5. HTTP 调用使用统一的 ApiClient（构造器注入）
6. 禁止字段级 @Autowired，使用 @RequiredArgsConstructor
7. DTO 用 Lombok @Value（替代 record），Entity 用 MyBatis-Plus @TableName
