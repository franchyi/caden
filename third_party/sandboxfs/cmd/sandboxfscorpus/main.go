package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func main() {
	root := flag.String("root", "", "new corpus directory")
	smallFiles := flag.Int("small-files", 10000, "number of small files")
	smallBytes := flag.Int("small-bytes", 1024, "bytes in each small file")
	largeMiBText := flag.String("large-mib", "1,100", "comma-separated large file sizes in MiB")
	flag.Parse()
	if *root == "" || *smallFiles < 0 || *smallBytes < 0 {
		fmt.Fprintln(os.Stderr, "sandboxfscorpus: valid --root, --small-files, and --small-bytes are required")
		os.Exit(2)
	}
	if _, err := os.Stat(*root); err == nil {
		fmt.Fprintf(os.Stderr, "sandboxfscorpus: refusing existing path %s\n", *root)
		os.Exit(1)
	} else if !os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "sandboxfscorpus: inspect root: %v\n", err)
		os.Exit(1)
	}
	largeMiB, err := parseSizes(*largeMiBText)
	if err != nil {
		fmt.Fprintf(os.Stderr, "sandboxfscorpus: %v\n", err)
		os.Exit(2)
	}
	if err := generate(*root, *smallFiles, *smallBytes, largeMiB); err != nil {
		fmt.Fprintf(os.Stderr, "sandboxfscorpus: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("generated %s with %d small files and large files %v MiB\n", *root, *smallFiles, largeMiB)
}

func parseSizes(text string) ([]int, error) {
	var sizes []int
	for _, field := range strings.Split(text, ",") {
		value, err := strconv.Atoi(strings.TrimSpace(field))
		if err != nil || value <= 0 {
			return nil, fmt.Errorf("invalid large file size %q", field)
		}
		sizes = append(sizes, value)
	}
	return sizes, nil
}

func generate(root string, smallFiles, smallBytes int, largeMiB []int) error {
	smallRoot := filepath.Join(root, "repository", "dependencies")
	largeRoot := filepath.Join(root, "repository", "large")
	if err := os.MkdirAll(smallRoot, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(largeRoot, 0o755); err != nil {
		return err
	}
	payload := make([]byte, smallBytes)
	for index := range payload {
		payload[index] = byte('a' + index%26)
	}
	for index := 0; index < smallFiles; index++ {
		directory := filepath.Join(smallRoot, fmt.Sprintf("d%04d", index/100))
		if err := os.MkdirAll(directory, 0o755); err != nil {
			return err
		}
		path := filepath.Join(directory, fmt.Sprintf("file-%06d.dat", index))
		if err := os.WriteFile(path, payload, 0o644); err != nil {
			return err
		}
	}
	block := make([]byte, 1<<20)
	for _, sizeMiB := range largeMiB {
		path := filepath.Join(largeRoot, fmt.Sprintf("large-%dm.bin", sizeMiB))
		file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
		if err != nil {
			return err
		}
		for index := 0; index < sizeMiB; index++ {
			block[0] = byte(index)
			if _, err := file.Write(block); err != nil {
				file.Close()
				return err
			}
		}
		if err := file.Sync(); err != nil {
			file.Close()
			return err
		}
		if err := file.Close(); err != nil {
			return err
		}
	}
	return os.WriteFile(filepath.Join(root, "repository", "sentinel.txt"), []byte("immutable-base\n"), 0o644)
}
