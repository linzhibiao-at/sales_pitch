package com.example.app.architecture;

import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;
import org.springframework.beans.factory.annotation.Autowired;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noFields;
import static com.tngtech.archunit.library.Architectures.layeredArchitecture;

@AnalyzeClasses(
    packages = "com.example.app",
    importOptions = ImportOption.DoNotIncludeTests.class
)
public class LayerDependencyTest {

    @ArchTest
    public static final ArchRule layered = layeredArchitecture()
        .consideringAllDependencies()
        .layer("Domain").definedBy("..domain..")
        .layer("Config").definedBy("..config..")
        .layer("Mapper").definedBy("..mapper..")
        .layer("Service").definedBy("..service..")
        .layer("Controller").definedBy("..controller..")
        .layer("Infrastructure").definedBy("..infrastructure..")

        .whereLayer("Controller").mayNotBeAccessedByAnyLayer()
        .whereLayer("Service").mayOnlyBeAccessedByLayers("Controller")
        .whereLayer("Mapper").mayOnlyBeAccessedByLayers("Service")
        .withOptionalLayers(true)
        .as(
            "❌ 层级依赖违规。\n" +
            "✅ FIX: Controller 必须经 Service，Service 通过 MyBatis-Plus Mapper 访问数据。\n" +
            "📖 See: docs/architecture/boundaries.md"
        )
        .allowEmptyShould(true);

    @ArchTest
    public static final ArchRule controllerMustNotUseMapper = noClasses()
        .that().resideInAPackage("..controller..")
        .should().dependOnClassesThat().resideInAPackage("..mapper..")
        .as(
            "❌ Controller 不得直接依赖 Mapper。\n" +
            "✅ FIX: 在 Service 中编排数据访问，Controller 仅持有 Service 引用。\n" +
            "📖 See: docs/architecture/boundaries.md"
        )
        .allowEmptyShould(true);

    @ArchTest
    public static final ArchRule noFieldInjection = noFields()
        .should().beAnnotatedWith(Autowired.class)
        .as(
            "❌ 禁止字段级 @Autowired。\n" +
            "✅ FIX: 构造器注入：\n" +
            "   @RequiredArgsConstructor\n" +
            "   public class UserService {\n" +
            "       private final UserMapper userMapper;\n" +
            "   }\n" +
            "📖 See: docs/conventions/di.md"
        )
        .allowEmptyShould(true);
}
