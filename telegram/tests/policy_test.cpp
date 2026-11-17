#include "stopscroll/stopscroll_policy.h"

#include <QtCore/QCoreApplication>
#include <iostream>
#include <stdexcept>

namespace {

auto checks = 0;

void Check(bool value, const char *description) {
	++checks;
	if (!value) {
		throw std::runtime_error(description);
	}
}

}

int main(int argc, char **argv) {
	const auto app = QCoreApplication(argc, argv);
	try {
		auto policy = Stopscroll::Policy();
		Check(!policy.ready(), "uninitialized policy");
		Check(policy.blocked(123, {"allowed"}), "fail closed before policy");
		const auto valid = QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":["123"]},{"username":"rybar","ids":[]}]})");
		Check(policy.update(valid), "load valid policy");
		Check(policy.ready(), "valid policy ready");
		Check(policy.blocked(99, {"ToPoR"}), "case-insensitive username");
		Check(policy.blocked(99, {"alias", "rybar"}), "multiple usernames");
		Check(policy.blocked(123, {"new_name"}), "ID survives rename");
		Check(!policy.blocked(999, {"topor_allowed"}), "no prefix matching");
		Check(!policy.blocked(999, {"mytopor"}), "no substring matching");
		Check(!policy.blocked(999, {}), "unlisted ID");
		policy.remember("rybar", 456);
		Check(policy.blocked(456, {"renamed"}), "learned ID blocks rename");
		Check(policy.update(valid), "reload policy");
		Check(policy.blocked(456, {}), "retain learned ID during publication delay");
		Check(!policy.recorded("rybar", 456), "unpublished learned ID needs observation");
		Check(policy.recorded("topor", 123), "published ID needs no observation");
		for (const auto &invalid : {
			QByteArray(""),
			QByteArray("[]"),
			QByteArray("null"),
			QByteArray(R"({"version":2,"channels":[]})"),
			QByteArray(R"({"version":"1","channels":[]})"),
			QByteArray(R"({"version":1,"channels":null})"),
			QByteArray(R"({"version":1,"channels":[null]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"TOPOR","ids":[]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor\n","ids":[]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":["123\n"]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor"}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":[123]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":["-100123"]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":["0"]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":["01"]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":["4503599627370496"]}]})"),
			QByteArray(R"({"version":1,"channels":[{"username":"topor","ids":[]},{"username":"topor","ids":[]}]})"),
			QByteArray(1024 * 1024 + 1, ' '),
		}) {
			Check(!policy.update(invalid), "reject invalid policy");
			Check(policy.ready() && policy.blocked(123, {}), "retain last valid policy");
			Check(policy.blocked(999, {"topor"}), "retain last valid username block");
		}
		Check(policy.update(R"({"version":1,"channels":[{"username":"rybar","ids":["4503599627370495"]}]})"), "maximum raw ID");
		Check(policy.blocked(4503599627370495ULL, {}), "match maximum raw ID");
		Check(!policy.blocked(123, {"topor"}), "removal takes effect");
		Check(policy.update(R"({"version":1,"channels":[]})"), "empty policy");
		Check(!policy.blocked(456, {"rybar"}), "removal clears learned IDs");
		policy.remember("rybar", 456);
		Check(!policy.blocked(456, {}), "cannot learn an unblocked name");
		std::cout << checks << " policy checks passed\n";
		return 0;
	} catch (const std::exception &error) {
		std::cerr << "Policy check failed: " << error.what() << '\n';
		return 1;
	}
}
