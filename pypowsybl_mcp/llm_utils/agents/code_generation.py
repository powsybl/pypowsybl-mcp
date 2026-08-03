#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import asyncio
import os

from agents import Agent, ModelSettings, RunConfig, Runner
from loguru import logger
from openai.types import Reasoning

from pypowsybl_mcp.llm_utils.agents.agent_utils import create_agent

AI_TIMEOUT = 90  # seconds

CODE_GENERATION_INSTRUCTIONS = """
You are an expert Python developer specializing in creating simple, compact, and practical code.

Your task is to generate a standalone, executable Python script that replicates the functionality of a sequence of MCP (Model Context Protocol) tool calls, without any dependency on the MCP server infrastructure.

## Core Requirements

1. **Extract and Adapt Core Logic**
   - Analyze the MCP server's tool implementations to understand their underlying functionality
   - Extract ONLY the essential business logic from each tool, removing all MCP-specific wrappers
   - Preserve the exact sequence and behavior of the original actions
   - Ensure the generated code produces equivalent results to the MCP tool chain

2. **Eliminate MCP Dependencies**
   - Remove all references to: MCP protocol, FastMCP, tool decorators, context objects, session management, server infrastructure
   - Replace MCP tool calls with direct API calls or inline code
   - Convert async patterns to sync (unless async is truly essential for the underlying logic)
   - Remove all request/response handling, routing, and HTTP endpoint code

3. **Generalize Parameters and Configuration**
   - Identify hardcoded values in the action sequence (IDs, paths, URLs, thresholds, etc.)
   - Convert these into:
     * Function parameters with clear names and type hints
     * Default values that match the original examples
   - Use the original specific values as examples in function signatures (as defaults)
   - Keep parameter documentation minimal but clear

4. **Maximize Simplicity and Compactness** ⚠️ CRITICAL
   - Write the SIMPLEST code that works correctly
   - INLINE logic directly instead of creating unnecessary helper functions
   - Use ONE main function that performs all actions in sequence
   - Avoid over-engineering: if something can be done in 3 lines instead of 10, do it in 3
   - Remove ALL unnecessary abstractions and wrapper functions
   - Eliminate intermediate variables that don't add clarity
   - Prefer straightforward procedural code over complex object-oriented patterns
   - Don't create classes unless absolutely necessary
   - Focus on RESULTS, not on showcasing programming techniques
   - Each line of code should serve a clear, necessary purpose

5. **Keep Documentation Minimal and Practical**
   - Add a brief docstring for the main function (purpose, parameters, returns)
   - Include inline comments ONLY for non-obvious logic or critical steps
   - Don't over-comment simple operations (e.g., don't comment `x = 5  # set x to 5`)
   - Remove boilerplate comments and verbose explanations
   - Keep it concise and focused

6. **Handle Errors Pragmatically**
   - Include ONLY essential error handling that prevents crashes
   - Use simple try-except blocks where necessary
   - Remove overly defensive checks and elaborate error handling
   - Prefer letting exceptions propagate naturally rather than catching everything
   - Include informative error messages for critical failures only

7. **Validation Checklist** (verify before outputting)
   - [ ] No MCP-specific imports or references remain
   - [ ] All actions from the sequence are implemented in order
   - [ ] Code is as short and simple as possible while being correct
   - [ ] No unnecessary functions, classes, or abstractions
   - [ ] Parameters are properly generalized with defaults matching examples
   - [ ] Code is executable without modifications (given proper inputs)
   - [ ] Variable names are clear and consistent
   - [ ] Only necessary dependencies are imported
   - [ ] Total line count is minimized without sacrificing correctness

## Output Format

Provide ONLY the final Python code with NO markdown code block markers (no ```python or ```):
- Imports at the top (only what's needed)
- One main function that executes all actions in sequence
- Helper functions ONLY if they significantly reduce code duplication (e.g., when the same tool is called multiple times with different parameters)
- if __name__ == "__main__" block for direct execution
- No additional explanations, markdown, or commentary
 
## Handling Repeated Tool Calls
 
If the Action Sequence calls the same tool multiple times:
1. If the tool's core logic is more than a single simple API call, extract it into a small local helper function to avoid duplicating the implementation details.
2. Call this helper function multiple times within the main function.
3. If the tool call is just a single line (e.g., `pp.loadflow.run_ac(network)`), just call it directly as needed.
 
## Example of Good vs Bad Code

❌ BAD (over-engineered):
```python
class NetworkManager:
    def __init__(self, network_type: str):
        self.network_type = network_type
        self.network = None

    def create_network(self):
        creators = {...}
        self.network = creators[self.network_type]()
        return self.network

    def get_bus_count(self):
        return len(self.network.get_buses())

def main():
    manager = NetworkManager("IEEE14")
    network = manager.create_network()
    count = manager.get_bus_count()
    print(f"Created {count} buses")
```

✅ GOOD (simple and direct):
```python
def main(network_type="IEEE14"):
    network = getattr(pp.network, f"create_{network_type.lower()}")()
    print(f"Created {len(network.get_buses())} buses")
    return network
```

Remember: The goal is WORKING CODE with MINIMUM LINES, not elegant abstractions.
"""

CODE_GENERATION_PROMPT = """
## Context: MCP Server Tool Implementations
The following code snippets show how the MCP tools are implemented. Use these as reference to extract the core logic (pypowsybl calls) for each action.
```python
{code}
```

## Input: Action Sequence
The following actions were executed using the MCP server's tools. Each action represents a tool call with its parameters:
```
{actions}
```

## Task
Generate a standalone Python script that performs these actions in sequence, following all instructions in the CODE_GENERATION_INSTRUCTIONS.

Important:
1. Identify which tool in the "MCP Server Tool Implementations" corresponds to each action in the "Action Sequence".
2. Extract the core logic from that tool (e.g., calls to `pp.network.create_ieee14`, `pp.loadflow.run_ac`, etc.).
3. Combine these into a single, cohesive script without any MCP dependencies.

Output only the Python code.
"""

ai_config = {
    "model_name": os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.1"),
    "api_key": os.getenv("OPENAI_API_KEY", ""),
    "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "reasoning_level": "high",
    "verbosity": "low",
    "max_turns": 10,
}
run_config = RunConfig(tracing_disabled=True)


def initialize_agent() -> Agent:
    model_settings = ModelSettings(
        reasoning=Reasoning(effort=ai_config["reasoning_level"]),
        verbosity=ai_config["verbosity"],
    )

    coding_agent = create_agent(
        name="coding_agent",
        model_name="gpt-5.1",
        api_key=ai_config["api_key"],
        base_url=ai_config["base_url"],
        instructions=CODE_GENERATION_INSTRUCTIONS,
        mcp_servers=[],
        tools=[],
        model_settings=model_settings,
    )
    return coding_agent


coding_agent = initialize_agent()


async def generate_code_from_macro(
    actions: str,
    reference_code: str = None,
) -> str:
    """
    Generate a standalone Python script from a sequence of tool actions.

    Takes a list of MCP tool calls (tool names and parameters) and generates
    executable Python code that performs the same operations directly without requiring the MCP server.

    Args:
        actions (str): List of actions to convert to code.
        reference_code: code to use as reference for imports and other context to generate the output code

    Returns:
        str: generated Python script.
    Notes:
        - Review and test generated code before production use
    """
    try:
        # invoke agent
        result = await asyncio.wait_for(
            Runner.run(
                input=CODE_GENERATION_PROMPT.format(
                    actions=actions, code=reference_code
                ),
                starting_agent=coding_agent,
                run_config=run_config,
                max_turns=ai_config["max_turns"],
            ),
            timeout=AI_TIMEOUT,
        )
        response = (
            result.final_output if hasattr(result, "final_output") else str(result)
        )
        return response
    except asyncio.TimeoutError:
        error_msg = f"Code generation timed out after {AI_TIMEOUT} seconds"
        logger.error(error_msg)
        return f"# Error: {error_msg}"
    except Exception as e:
        error_msg = f"Error during code generation: {str(e)}"
        logger.error(error_msg)
        return f"# Error: {error_msg}"
