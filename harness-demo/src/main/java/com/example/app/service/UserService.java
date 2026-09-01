package com.example.app.service;

import com.example.app.domain.dto.UserResponse;
import com.example.app.domain.model.User;
import com.example.app.mapper.UserMapper;
import com.example.app.service.exception.UserNotFoundException;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class UserService {

    private final UserMapper userMapper;

    public UserResponse getById(Long id) {
        User user = userMapper.selectById(id);
        if (user == null) {
            throw new UserNotFoundException(id);
        }
        return new UserResponse(user.getId(), user.getName(), user.getEmail());
    }
}
