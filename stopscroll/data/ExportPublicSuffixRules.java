// Reads the packed rules in Guava 33.7.1-android. See README.md for provenance.
package com.google.thirdparty.publicsuffix;

import java.nio.charset.StandardCharsets;
import java.util.SortedSet;
import java.util.TreeSet;

public final class ExportPublicSuffixRules {
    private static final PublicSuffixTrie TRIE = PublicSuffixPatterns.TRIE;

    public static void main(String[] args) throws Exception {
        SortedSet<String> rules = new TreeSet<>();
        collect(0, "", rules);
        String header = "# Exported from com.google.guava:guava:33.7.1-android\n"
                + "# Derived from the Public Suffix List under MPL-2.0. See README.md.\n";
        System.out.write((header + String.join("\n", rules) + "\n").getBytes(StandardCharsets.UTF_8));
    }

    private static void collect(int index, String domain, SortedSet<String> rules) {
        int node = index * PublicSuffixTrie.NODE_SIZE;
        int firstChild = TRIE.trieData.charAt(node + 1);
        int flags = TRIE.trieData.charAt(node + 2);
        if (index != 0) {
            if ((flags >> PublicSuffixTrie.EXACT_SHIFT & PublicSuffixTrie.TYPE_MASK) != 0) {
                rules.add(domain);
            }
            if ((flags >> PublicSuffixTrie.WILDCARD_SHIFT & PublicSuffixTrie.TYPE_MASK) != 0) {
                rules.add("*." + domain);
            }
            if ((flags >> PublicSuffixTrie.EXCLUSION_SHIFT & PublicSuffixTrie.EXCLUSION_MASK) != 0) {
                rules.add("!" + domain);
            }
        }
        int childCount = flags & PublicSuffixTrie.CHILDREN_MASK;
        for (int child = firstChild; child < firstChild + childCount; child++) {
            int labelStart = TRIE.trieData.charAt(child * PublicSuffixTrie.NODE_SIZE);
            int length = TRIE.stringPool.charAt(labelStart);
            StringBuilder label = new StringBuilder(length);
            for (int offset = 1; offset <= length; offset++) {
                label.append(TRIE.stringPool.charAt(labelStart + offset));
            }
            collect(child, domain.isEmpty() ? label.toString() : label + "." + domain, rules);
        }
    }
}
