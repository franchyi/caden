def parse_memory(mem_str: str) -> float | None:
    """Parse memory string to MB."""
    mem_str = mem_str.strip()
    mem_lower = mem_str.lower()
    try:
        if mem_lower.endswith("gib"):
            return float(mem_str[:-3]) * 1024
        elif mem_lower.endswith("mib"):
            return float(mem_str[:-3])
        elif mem_lower.endswith("kib"):
            return float(mem_str[:-3]) / 1024
        elif mem_lower.endswith("gb"):
            return float(mem_str[:-2]) * 1000
        elif mem_lower.endswith("mb"):
            return float(mem_str[:-2])
        elif mem_lower.endswith("kb"):
            return float(mem_str[:-2]) / 1000
        elif mem_lower.endswith("g"):
            return float(mem_str[:-1]) * 1024
        elif mem_lower.endswith("m"):
            return float(mem_str[:-1])
        elif mem_lower.endswith("k"):
            return float(mem_str[:-1]) / 1024
        elif mem_lower.endswith("b"):
            return float(mem_str[:-1]) / (1024 * 1024)
        return float(mem_str)
    except Exception as e:
        print(f"Failed to parse memory string {mem_str}: {e}")
        return None

def parse_cpu(cpu_str: str) -> float | None:
    """Parse CPU string to float percentage."""
    cpu_str = cpu_str.strip()
    try:
        return float(cpu_str.replace("%", ""))
    except Exception as e:
        print(f"Failed to parse CPU string {cpu_str}: {e}")
        return None

def parse_cpu_limit_from_cpuset(cpuset_str: str) -> float:
    """Parse CPU limit as a floating point percentage from a cpuset_cpus str (e.g. 0-15)"""
    splits = cpuset_str.split("-")
    if len(splits) == 1:
        return 1.00
    elif len(splits) == 2:
        lhs, rhs = int(splits[0]), int(splits[1])
        return float(rhs - lhs + 1)
    else:
        raise ValueError("Invalid cpuset format: expecting either a single CPU ID or a range e..g 0-15")
    
    