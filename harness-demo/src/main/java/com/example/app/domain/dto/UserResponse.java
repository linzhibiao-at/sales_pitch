package com.example.app.domain.dto;

import lombok.Value;

@Value
public class UserResponse {
    Long id;
    String name;
    String email;
}
