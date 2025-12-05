import sys
from typing import Annotated, Iterator, Any, Optional

from talkpipe import segment, register_segment
from talkpipe.pipe.basic import toDict
from talkpipe.util.data_manipulation import compileLambda


@register_segment("diagPrint")
@segment()
def DiagPrint(
    items: Iterator[Any],
    field_list: Annotated[
        Optional[str],
        "Comma-separated fields to extract in form 'field[:new_name],...' where _ means the whole item"
    ] = None,
    expression: Annotated[
        Optional[str],
        "A Python expression using 'item' as the variable (e.g., 'item * 2')"
    ] = None,
    use_stderr: Annotated[
        bool,
        "If True, output to stderr instead of stdout"
    ] = False,
) -> Iterator[Any]:
    """
    A segment that prints items for diagnostic purposes.

    Output can be directed to stdout (default) or stderr.
    """
    output_file = sys.stderr if use_stderr else sys.stdout

    if expression:
        f = compileLambda(expression)

    for item in items:
        print("================================", file=output_file)
        print(f"Type: {type(item)}", file=output_file)
        print(f"Value: {item}", file=output_file)
        if field_list:
            print("-------\nFields:", file=output_file)
            item_dict = toDict(item, field_list=field_list)
            for key, value in item_dict.items():
                print(f"{key}: {value}", file=output_file)
        if expression:
            print("-------\nExpression:", file=output_file)
            print(f"Value: {f(item)}", file=output_file)
        yield item