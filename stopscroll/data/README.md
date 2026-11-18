# Pinned public suffix rules

The desktop Android policy validator uses the exact, wildcard and exception rules
from Guava 33.7.1-android, including private suffixes. The sorted UTF-8 export
follows the trie representation in the Android build's dependency. A domain with
an unlisted suffix is eligible if it meets the policy's other constraints.

| Input or generated file | SHA-256 |
| --- | --- |
| [Guava 33.7.1-android JAR](https://repo.maven.apache.org/maven2/com/google/guava/guava/33.7.1-android/guava-33.7.1-android.jar) | `603942ca694ea40060a0a54dd620c05668c904736bb4e16c80760e77e69c107d` |
| [PublicSuffixPatterns.java](https://raw.githubusercontent.com/google/guava/v33.7.1/android/guava/src/com/google/thirdparty/publicsuffix/PublicSuffixPatterns.java) | `65421cb019cbda19e2453574663fd532f69874bc610137151ce59e35b0c3f75d` |
| [PublicSuffixTrie.java](https://raw.githubusercontent.com/google/guava/v33.7.1/android/guava/src/com/google/thirdparty/publicsuffix/PublicSuffixTrie.java) | `8ed4cd5fe9f0b672d066c3864f22f7f13227af40d8bef57d819b34bfa4403985` |
| [public-suffix-rules.txt](public-suffix-rules.txt) | `d212c108ac3bd2d1d9ec8997e18a430b7e31a55558ba68b969ae517093b048fb` |

From the repository root, set `GUAVA_JAR` to the pinned JAR, verify its SHA-256
against the table, then regenerate the file with a JDK.

```sh
classes=$(mktemp -d)
javac -cp "$GUAVA_JAR" -d "$classes" stopscroll/data/ExportPublicSuffixRules.java
java -cp "$classes:$GUAVA_JAR" com.google.thirdparty.publicsuffix.ExportPublicSuffixRules \
    > "$classes/public-suffix-rules.txt"
cmp "$classes/public-suffix-rules.txt" stopscroll/data/public-suffix-rules.txt
```

The rule data comes from the Public Suffix List and is distributed under
[MPL-2.0](LICENSE-MPL-2.0.txt). The Guava Authors distribute the source trie and
its generated Java representation under [Apache-2.0](LICENSE-APACHE-2.0.txt).
[Public Suffix List project](https://publicsuffix.org/list/)
