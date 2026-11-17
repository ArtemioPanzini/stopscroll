cmake_minimum_required(VERSION 3.25)
project(stopscroll_policy_tests LANGUAGES CXX)
enable_testing()
find_package(Qt6 REQUIRED COMPONENTS Core)
set(source_root "${CMAKE_CURRENT_SOURCE_DIR}/../tdesktop/Telegram/SourceFiles")
add_executable(stopscroll_policy_tests
    policy_test.cpp
    "${source_root}/stopscroll/stopscroll_policy.cpp"
)
target_compile_features(stopscroll_policy_tests PRIVATE cxx_std_20)
target_include_directories(stopscroll_policy_tests PRIVATE "${source_root}")
target_link_libraries(stopscroll_policy_tests PRIVATE Qt6::Core)
add_test(NAME stopscroll_policy COMMAND stopscroll_policy_tests)
add_executable(stopscroll_policy_probe
    policy_probe.cpp
    "${source_root}/stopscroll/stopscroll_policy.cpp"
)
target_compile_features(stopscroll_policy_probe PRIVATE cxx_std_20)
target_include_directories(stopscroll_policy_probe PRIVATE "${source_root}")
target_link_libraries(stopscroll_policy_probe PRIVATE Qt6::Core)
