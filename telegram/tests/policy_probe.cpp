#include "stopscroll/stopscroll_policy.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QFile>
#include <iostream>

int main(int argc, char **argv) {
	const auto app = QCoreApplication(argc, argv);
	if (argc < 3) {
		std::cerr << "Usage: stopscroll_policy_probe POLICY NAME...\n";
		return 2;
	}
	auto file = QFile(QString::fromLocal8Bit(argv[1]));
	auto policy = Stopscroll::Policy();
	if (!file.open(QIODevice::ReadOnly) || !policy.update(file.readAll())) {
		std::cerr << "Cannot load policy\n";
		return 1;
	}
	for (auto i = 2; i < argc; ++i) {
		const auto name = QString::fromLocal8Bit(argv[i]);
		if (!policy.blocked(0, { name })) {
			std::cerr << "Missing block: " << name.toStdString() << '\n';
			return 1;
		}
	}
	if (policy.blocked(0, { "stopscroll_allowed_test" })) {
		std::cerr << "Unexpected block of an unlisted name\n";
		return 1;
	}
	std::cout << (argc - 2) << " configured blocks verified in C++\n";
	return 0;
}
