#pragma once

struct lua_State;

// These functions are already owned by REFramework's ScriptRunner. Keeping the
// declarations here avoids exposing ScriptRunner C++ types through the ABI.
lua_State* reframework_create_script_state();
void reframework_destroy_script_state(lua_State* lua_state);
